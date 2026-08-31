"""Declared entity minting for ChatGPT turns (PLAN_CHATGPT_IMPORT.md Sprint 4).

A ChatGPT turn carries more than its text. The export declares, per message,
which pages were cited, which searches were run, which canvas document was being
written and which files were attached — and the reader already lifts all of it
onto ``ai_chat_messages.metadata_json``. Until now none of it became a node, so
the graph could not answer "what sources inform me" even though the answer was
sitting in a column.

This is the same class the 1.3.36 density pass ranked strongest: read from a
structured column, no model, no confidence to discount.

Three judgements are encoded here, and each one is a decision not a detail:

**Hosts, not URLs.** 1,359 distinct URLs would mint 1,359 leaf nodes that
connect to nothing and say nothing. The *host* is the source — ``docs.python.org``
is a thing you read repeatedly — so hosts are minted and the full URL is kept as
the mention's surface text. Subdomains stay distinct: reading Google Cloud's
docs is not reading google.com.

**Search queries are not minted.** They are declared, and they are read — they
ride on the exposure edge and on the mention that the search produced — but a
sentence is not an entity. Minting "Google Cloud roles/compute.instanceAdmin.v1
permissions create start stop delete VM instances" as a node would add a row, not
density. The entities *inside* a query reach the graph through the citations the
query returned.

**Attachments must be named by a human.** A ChatGPT export names half its
attachments with a UUID. ``proposal-v3.pdf`` is a document; ``bb2b7a54-d935-4ac1
-82d2-af0ff5df204c.png`` is a filename the system made up, and minting it would
put a hash in the graph.

Pure stdlib. The registry in ``declared_mappings`` dispatches to this.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

# Spine entity types minted here. Both are new to the vocabulary in
# ``resolver._NER_TYPE_MAP``; declared rows keep their type verbatim, so no
# mapping is needed, but a UI that switches on entity_type should learn them.
TYPE_WEB_SOURCE = "web_source"
TYPE_DOCUMENT = "document"

# Owner edges. Exposure is not authorship: a page the assistant fetched and
# showed is something the owner was exposed to, and the provenance layer must
# never read it as something they said.
EDGE_EXPOSED_TO = "exposed_to"
EDGE_AUTHORED = "authored"

# Hosts that identify no source: loopback, bare addresses, and the redirectors
# a search tool leaves behind.
_HOST_DENYLIST = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "example.com"})
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# A filename a person chose, versus one a system generated.
_UUID_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_HEX_NAME = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_MIN_DOCUMENT_NAME = 3


def host_of(url: str) -> str:
    """The source a URL names: hostname, lowercased, without ``www.``.

    Returns "" for anything that does not identify a readable source.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    if not host or "." not in host or host in _HOST_DENYLIST or _IPV4.match(host):
        return ""
    return host


def is_named_by_a_human(filename: str) -> bool:
    """Did a person choose this filename, or did a machine?"""
    name = str(filename or "").strip()
    if len(name) < _MIN_DOCUMENT_NAME:
        return False
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if _UUID_NAME.match(stem) or _HEX_NAME.match(stem):
        return False
    # "file_00000000abc" and friends: no letters a person would have typed.
    return bool(re.search(r"[A-Za-z]{3,}", stem))


def _metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    """``metadata_json`` arrives as a dict at map time and a string from SQL."""
    value = record.get("metadata_json")
    if value is None:
        value = record.get("metadata")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _row(
    surface: str,
    entity_type: str,
    *,
    record: Dict[str, Any],
    record_id: str,
    event_at: Optional[str],
    self_edge: Optional[str] = None,
    surface_detail: Optional[str] = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "message_id": record_id,
        "record_id": record_id,
        "source_id": record.get("source_id"),
        "event_at": event_at,
        "canonical_table": record.get("_table") or record.get("canonical_table") or "ai_chat_messages",
        "entity_text": surface,
        "entity_type": entity_type,
        "confidence": 1.0,
        "provider": "declared",
        "model": None,
    }
    if self_edge:
        row["self_edge"] = self_edge
    if surface_detail:
        # What the graph shows is the host; what the evidence says is the URL.
        row["surface_detail"] = surface_detail
    return row


def _citation_urls(metadata: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for citation in metadata.get("citation_urls") or []:
        if isinstance(citation, str) and citation.strip():
            urls.append(citation.strip())
    return urls


def _canvas_title(metadata: Dict[str, Any]) -> str:
    canvas = metadata.get("canvas")
    if not isinstance(canvas, dict):
        return ""
    title = canvas.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return ""


def _attachment_names(metadata: Dict[str, Any]) -> Iterable[str]:
    for item in metadata.get("attachments") or []:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and is_named_by_a_human(name):
                yield name.strip()


def declared_rows(
    record: Dict[str, Any],
    *,
    record_id: str,
    event_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """NER-result-shaped rows minted from one turn's declared metadata.

    Shape matches ``declared_mappings.extract_declared_entities`` exactly, so the
    entities job resolves these through the same declared path — at declared
    confidence, with the type kept verbatim.
    """
    if not record_id:
        return []
    metadata = _metadata(record)
    if not metadata:
        return []

    rows: List[Dict[str, Any]] = []
    seen: set = set()

    # Which search produced this message's citations, if any. Carried on the
    # mention rather than minted: it is evidence, not an entity.
    queries = [q for q in (metadata.get("search_queries") or []) if isinstance(q, str) and q.strip()]
    query_note = queries[0].strip() if queries else ""

    for url in _citation_urls(metadata):
        host = host_of(url)
        if not host:
            continue
        key = (host, TYPE_WEB_SOURCE)
        if key in seen:
            continue
        seen.add(key)
        detail = f"{url} — searched: {query_note}" if query_note else url
        rows.append(_row(host, TYPE_WEB_SOURCE, record=record, record_id=record_id,
                         event_at=event_at, self_edge=EDGE_EXPOSED_TO, surface_detail=detail))

    canvas = _canvas_title(metadata)
    if canvas:
        key = (canvas.lower(), TYPE_DOCUMENT)
        if key not in seen:
            seen.add(key)
            rows.append(_row(canvas, TYPE_DOCUMENT, record=record, record_id=record_id,
                             event_at=event_at, self_edge=EDGE_AUTHORED))

    for name in _attachment_names(metadata):
        key = (name.lower(), TYPE_DOCUMENT)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_row(name, TYPE_DOCUMENT, record=record, record_id=record_id,
                         event_at=event_at, self_edge=EDGE_AUTHORED))

    return rows


def coverage(records: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """What this lane would mint over a batch — the Sprint 4 measurement."""
    hosts: set = set()
    documents: set = set()
    urls = 0
    queries = 0
    skipped_machine_names = 0
    for record in records:
        metadata = _metadata(record)
        if not metadata:
            continue
        for url in _citation_urls(metadata):
            urls += 1
            host = host_of(url)
            if host:
                hosts.add(host)
        queries += len([q for q in (metadata.get("search_queries") or []) if isinstance(q, str) and q.strip()])
        title = _canvas_title(metadata)
        if title:
            documents.add(title.lower())
        for item in metadata.get("attachments") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                if is_named_by_a_human(item["name"]):
                    documents.add(item["name"].strip().lower())
                else:
                    skipped_machine_names += 1
    return {
        "citation_urls": urls,
        "web_sources": len(hosts),
        "documents": len(documents),
        "search_queries_read": queries,
        "attachments_skipped_machine_named": skipped_machine_names,
    }
