"""Seeded-SQLite full-ingest CER corpus (plan §A.1 full-path mode).

Unlike the in-memory corpus (which pre-populates disclosure columns), this drives records
through the REAL platform privacy layer (`run_privacy_disclosure_layer`) so the disclosure
columns and NSFW tags are genuinely computed and written to SQLite, and grantee reads go
through the REAL SQL disclosure spec (`coalesce(content_disclosure, '[disclosure pending]')`).

Only the model is faked — a deterministic redactor that strips the planted canary tokens
(and email/phone) but keeps the rest, mirroring what a real PII model would do. This isolates
the plumbing (column writes + SQL read + fail-closed pending) from model quality.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from topos.disclosure.privacy_layer import run_privacy_disclosure_layer
from topos.query.manifest import ScopeResolutionManifest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore

from .corpus import Canary

MESSAGES_TABLE = "conversation_messages"
MESSAGES_SCOPE = "messages:read"
SQLITE_CER_QUERY_PHRASE = "quarterly report"

# One token per class; each appears only in a record's raw content.
_S_EMAIL = "zx-sql-email-6602@example-priv.net"
_S_PHONE = "+1-555-0288"
_S_RAW = "sql-secret-body-7741"
_S_NSFW = "sql-nsfw-marker-9003"
_S_PENDING = "sql-pending-secret-1200"

_EMAIL_RE = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()-]{7,}\d")


class _DeterministicPrivacyClient:
    """Fake PII/NSFW model: strips known canary tokens + email/phone, keeps the rest.
    NSFW is flagged when a record's text carries the NSFW marker."""

    def __init__(self, strip_tokens: List[str]) -> None:
        self._strip = strip_tokens

    def _redact(self, text: str) -> str:
        out = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        out = _PHONE_RE.sub("[REDACTED_PHONE]", out)
        for tok in self._strip:
            out = out.replace(tok, "[REDACTED]")
        return out

    async def redact_batch(self, items: List[Dict[str, Any]], *, transform_id: str = "pii_redaction") -> Dict[str, Any]:
        return {
            "items": [{"id": it.get("id"), "text": self._redact(str(it.get("text") or ""))} for it in items],
            "model": "fake-deterministic-redactor",
            "privacy_layer_version": "test",
        }

    async def classify_nsfw_batch(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        out = []
        for it in items:
            text = str(it.get("text") or "")
            is_nsfw = _S_NSFW in text or "nsfw" in text.lower()
            out.append({"id": it.get("id"), "nsfw": is_nsfw, "score": 0.95 if is_nsfw else 0.0})
        return {"items": out, "model": "fake-nsfw"}


@dataclass
class SqliteCerCorpus:
    conn: Any
    bundle: Any
    canaries: List[Canary]
    scope_id: str
    manifest: ScopeResolutionManifest


def _insert(conn, message_id: str, content: str) -> None:
    conn.execute(
        "INSERT INTO conversation_messages "
        "(message_id, conversation_id, dataset_id, sender_type, content, source_id, event_at) "
        "VALUES (?, 'c1', 'default', 'human', ?, 'imessage', '2026-06-01T12:00:00Z')",
        (message_id, content),
    )


def _raw_manifest() -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id=MESSAGES_SCOPE,
        primary_dimensions=["Relationships", "Memory"],
        canonical_tables=[MESSAGES_TABLE],
        access_mode_ceiling="raw",
        default_source_id="imessage",
    )


def build_sqlite_cer_corpus() -> SqliteCerCorpus:
    """Seed conversation_messages, run the real privacy layer on all but the 'pending' record,
    and return a bundle over the live SQLite connection."""
    from tests.gap.remediation.remediation_helpers import sqlite_conn

    conn = sqlite_conn()
    SQLiteCanonicalStore(conn)  # applies the disclosure/NSFW migrations

    phrase = SQLITE_CER_QUERY_PHRASE
    _insert(conn, "sq-email", f"{phrase}: reach me at {_S_EMAIL} or {_S_PHONE}")
    _insert(conn, "sq-raw", f"{phrase}: the vault code is {_S_RAW}")
    _insert(conn, "sq-nsfw", f"{phrase}: explicit {_S_NSFW}")
    _insert(conn, "sq-pending", f"{phrase}: unprocessed secret {_S_PENDING}")
    conn.commit()

    # Full ingest privacy layer on every record EXCEPT the pending one (left unprocessed so its
    # disclosure column stays NULL → grantee reads must fail closed to the placeholder).
    processed = [
        {"_table": MESSAGES_TABLE, "message_id": "sq-email", "content": f"{phrase}: reach me at {_S_EMAIL} or {_S_PHONE}"},
        {"_table": MESSAGES_TABLE, "message_id": "sq-raw", "content": f"{phrase}: the vault code is {_S_RAW}"},
        {"_table": MESSAGES_TABLE, "message_id": "sq-nsfw", "content": f"{phrase}: explicit {_S_NSFW}"},
    ]
    client = _DeterministicPrivacyClient(strip_tokens=[_S_EMAIL, _S_PHONE, _S_RAW, _S_NSFW, _S_PENDING])
    asyncio.run(run_privacy_disclosure_layer(conn, processed, client=client))
    conn.commit()

    bundle = AdapterFactory.create("local_database", conn=conn)
    canaries = [
        Canary("sq-email", "email_phone", _S_EMAIL),
        Canary("sq-email-phone", "email_phone", _S_PHONE),
        Canary("sq-raw", "raw_body", _S_RAW),
        Canary("sq-nsfw", "nsfw", _S_NSFW),
        Canary("sq-pending", "pending_disclosure", _S_PENDING),
    ]
    return SqliteCerCorpus(conn=conn, bundle=bundle, canaries=canaries, scope_id=MESSAGES_SCOPE, manifest=_raw_manifest())
