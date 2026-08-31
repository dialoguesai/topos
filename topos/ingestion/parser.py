"""File parsers for ingestion files (JSONL, JSON, CSV)."""

from __future__ import annotations

import csv
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

from .parsers.chatgpt_export import (
    DropLedger,
    ExportOptions,
    is_conversation,
    iter_export,
)

logger = logging.getLogger("topos.ingestion.parser")


def _strip_json_comments(content: str) -> str:
    """Remove // and /* */ comments while preserving string literals."""
    out: list[str] = []
    i = 0
    n = len(content)
    in_string = False
    escaped = False

    while i < n:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < n else ""

        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            i += 2
            while i < n and content[i] not in "\r\n":
                i += 1
            continue

        if ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (content[i] == "*" and content[i + 1] == "/"):
                if content[i] in "\r\n":
                    out.append(content[i])
                i += 1
            i += 2 if i < n - 1 else 0
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _load_json_with_optional_comments(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        stripped = _strip_json_comments(content)
        if stripped != content:
            try:
                logger.info("Parsed JSON payload after stripping comments")
                return json.loads(stripped)
            except json.JSONDecodeError as commented_exc:
                raise ValueError(
                    "Failed to parse JSON file: "
                    f"{commented_exc.msg} (line {commented_exc.lineno}, column {commented_exc.colno})"
                ) from commented_exc
        raise ValueError(
            f"Failed to parse JSON file: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc


async def parse_jsonl_stream(file_stream: AsyncIterator[bytes]) -> AsyncIterator[Dict[str, Any]]:
    buffer = b""
    line_num = 0
    async for chunk in file_stream:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            line_num += 1
            try:
                record = json.loads(line.decode("utf-8"))
                yield record
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse JSONL line %d: %s", line_num, exc)
                continue
    if buffer.strip():
        line_num += 1
        try:
            record = json.loads(buffer.decode("utf-8"))
            yield record
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse JSONL line %d: %s", line_num, exc)


async def parse_json_stream(
    file_stream: AsyncIterator[bytes],
    options: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    chunks = []
    async for chunk in file_stream:
        chunks.append(chunk)
    content = b"".join(chunks).decode("utf-8")
    data = _load_json_with_optional_comments(content)
    if isinstance(data, list):
        # Check if this is a ChatGPT conversation array
        if data and is_conversation(data[0]):
            for record in _read_chatgpt_export(data, options):
                yield record
        else:
            # Regular array - yield records as-is
            for record in data:
                yield record
    elif isinstance(data, dict):
        # Check if single conversation object
        if is_conversation(data):
            for record in _read_chatgpt_export(data, options):
                yield record
        elif isinstance(data.get("browsing_history"), list):
            # Demo browser-history payloads wrap visit rows under a top-level key.
            # Flatten to per-visit records so source parsers can validate normally.
            owner_user_id = data.get("user_id")
            for record in data.get("browsing_history") or []:
                if isinstance(record, dict):
                    if owner_user_id and "user_id" not in record:
                        record = {**record, "user_id": owner_user_id}
                    yield record
        else:
            yield data
    else:
        raise ValueError(f"JSON must be array or object, got {type(data)}")


def _read_chatgpt_export(data: Any, options: Optional[Dict[str, Any]]):
    """Read a ChatGPT export with the import's own policy, and say what it left.

    The ledger is logged rather than returned because ``parse_file`` yields
    records; the job report reads the same counters from the manager.
    """
    export_options = ExportOptions.from_payload(options)
    ledger = DropLedger()
    yield from iter_export(data, export_options, ledger)
    stats = ledger.as_dict()
    logger.info(
        "[PIPELINE:PARSER] ChatGPT export read: %s/%s conversations, %s turns from %s nodes; dropped %s",
        stats["conversations_kept"],
        stats["conversations_seen"],
        stats["turns_emitted"],
        stats["message_nodes"],
        stats["dropped"],
    )


async def parse_csv_stream(file_stream: AsyncIterator[bytes], delimiter: str = ",") -> AsyncIterator[Dict[str, Any]]:
    chunks = []
    async for chunk in file_stream:
        chunks.append(chunk)
    content = b"".join(chunks).decode("utf-8")
    reader = csv.DictReader(content.splitlines(), delimiter=delimiter)
    for row in reader:
        yield row


async def parse_file(
    file_stream: AsyncIterator[bytes],
    file_format: str,
    options: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """``options`` is the job's ``ingest_options`` blob: date window and
    inclusion policy chosen at import time. Formats that have no policy ignore
    it."""
    format_lower = file_format.lower()
    if format_lower in {"jsonl", "ndjson"}:
        async for record in parse_jsonl_stream(file_stream):
            yield record
    elif format_lower == "json":
        async for record in parse_json_stream(file_stream, options):
            yield record
    elif format_lower == "csv":
        async for record in parse_csv_stream(file_stream):
            yield record
    else:
        raise ValueError(f"Unsupported file format: {file_format}")
