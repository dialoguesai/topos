#!/usr/bin/env python3
"""Ledger for one ChatGPT export: what we read, what we dropped, what we ignore.

The instrument PLAN_CHATGPT_IMPORT.md Sprint 0 asks for. It answers three
questions about a real export without touching a database:

  1. What does each extractor emit, and how much of it is blank or duplicated?
  2. Which declared columns does the export carry that we never read?
  3. What did the date window keep?

Run it on both extractors to get a before/after in one command:

    python scripts/chatgpt_import_report.py --input ../conversations.json --compare
    python scripts/chatgpt_import_report.py --input ~/Downloads/chatgpt-export \\
        --months 6 --json
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topos.ingestion.parsers.chatgpt_export import (  # noqa: E402
    DropLedger,
    ExportOptions,
    active_path_ids,
    conversation_activity,
    is_conversation,
    iter_export,
)

CONVERSATIONS_FILENAME = "conversations.json"

# Declared columns worth auditing for coverage. "read" means the v3 extractor
# puts the value somewhere a downstream consumer can reach it.
CONVERSATION_COLUMNS = (
    "title",
    "create_time",
    "update_time",
    "default_model_slug",
    "gizmo_id",
    "memory_scope",
    "is_starred",
    "is_archived",
    "is_do_not_remember",
    "conversation_template_id",
    "voice",
)
MESSAGE_METADATA_COLUMNS = (
    "citations",
    "search_result_groups",
    "search_queries",
    "attachments",
    "canvas",
    "dictation",
    "model_slug",
    "selected_github_repos",
    "selected_sources",
    "command",
    "finish_details",
)
# Facets the v3 extractor lands on the turn record's ``_metadata``.
V3_READS_CONVERSATION = {
    "title",
    "create_time",
    "update_time",
    "default_model_slug",
    "gizmo_id",
    "memory_scope",
    "is_starred",
    "is_archived",
    "is_do_not_remember",
    "conversation_template_id",
}
V3_READS_MESSAGE = {
    "citations",
    "search_result_groups",
    "search_queries",
    "attachments",
    "canvas",
    "dictation",
    "model_slug",
}


def resolve_export(path: Path) -> Tuple[Any, str]:
    """Accept a conversations.json, an export folder, or the export .zip."""
    if path.is_dir():
        candidate = path / CONVERSATIONS_FILENAME
        if not candidate.is_file():
            matches = sorted(path.rglob(CONVERSATIONS_FILENAME))
            if not matches:
                raise SystemExit(f"No {CONVERSATIONS_FILENAME} found under {path}")
            candidate = matches[0]
        return json.loads(candidate.read_text(encoding="utf-8")), str(candidate)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == CONVERSATIONS_FILENAME]
            if not names:
                raise SystemExit(f"No {CONVERSATIONS_FILENAME} inside {path}")
            name = min(names, key=lambda n: n.count("/"))
            with archive.open(name) as handle:
                return json.loads(handle.read().decode("utf-8")), f"{path}!{name}"
    return json.loads(path.read_text(encoding="utf-8")), str(path)


def legacy_records(payload: Any) -> List[Dict[str, Any]]:
    from topos.ingestion.parsers.chatgpt_conversation_flattener import flatten_conversation_array

    conversations = payload if isinstance(payload, list) else [payload]
    return list(flatten_conversation_array(conversations, include_system=False))


@dataclass
class ExtractionStats:
    label: str
    records: int
    empty: int
    by_role: Dict[str, int]
    by_content_type: Dict[str, int]
    chars_by_role: Dict[str, int]
    duplicate_records: int
    ledger: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "records": self.records,
            "empty_content_records": self.empty,
            "duplicate_records": self.duplicate_records,
            "by_role": self.by_role,
            "by_content_type": self.by_content_type,
            "chars_by_role": self.chars_by_role,
            "ledger": self.ledger,
        }


def summarise(label: str, records: List[Dict[str, Any]], ledger: Optional[DropLedger]) -> ExtractionStats:
    by_role: Counter = Counter()
    by_ct: Counter = Counter()
    chars: Counter = Counter()
    empty = 0
    fingerprints: Counter = Counter()
    for record in records:
        content = str(record.get("content") or "")
        role = str(record.get("role") or "?")
        meta = record.get("_metadata") or {}
        by_role[role] += 1
        by_ct[str(meta.get("content_type") or "?")] += 1
        chars[role] += len(content)
        if not content.strip():
            empty += 1
        else:
            fingerprints[(role, content[:200])] += 1
    duplicates = sum(count - 1 for count in fingerprints.values() if count > 1)
    return ExtractionStats(
        label=label,
        records=len(records),
        empty=empty,
        by_role=dict(by_role.most_common()),
        by_content_type=dict(by_ct.most_common()),
        chars_by_role=dict(chars.most_common()),
        duplicate_records=duplicates,
        ledger=ledger.as_dict() if ledger else None,
    )


def declared_coverage(conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Which declared columns the export fills in, and whether we read them."""
    conv_present: Counter = Counter()
    msg_present: Counter = Counter()
    distinct_urls: set[str] = set()
    search_queries = 0
    attachments = 0
    canvases = 0

    for conversation in conversations:
        for column in CONVERSATION_COLUMNS:
            if conversation.get(column) not in (None, "", [], {}):
                conv_present[column] += 1
        for node in (conversation.get("mapping") or {}).values():
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            for column in MESSAGE_METADATA_COLUMNS:
                if metadata.get(column) not in (None, "", [], {}, False):
                    msg_present[column] += 1
            for citation in metadata.get("citations") or []:
                if isinstance(citation, dict):
                    url = (citation.get("metadata") or {}).get("url")
                    if isinstance(url, str) and url:
                        distinct_urls.add(url)
            for group in metadata.get("search_result_groups") or []:
                for entry in (group or {}).get("entries") or []:
                    if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                        distinct_urls.add(entry["url"])
            search_queries += len(metadata.get("search_queries") or [])
            attachments += len(metadata.get("attachments") or [])
            if metadata.get("canvas"):
                canvases += 1

    return {
        "conversation_columns": [
            {"column": column, "present_on": conv_present.get(column, 0), "read_by_v3": column in V3_READS_CONVERSATION}
            for column in CONVERSATION_COLUMNS
        ],
        "message_metadata_columns": [
            {"column": column, "present_on": msg_present.get(column, 0), "read_by_v3": column in V3_READS_MESSAGE}
            for column in MESSAGE_METADATA_COLUMNS
        ],
        "distinct_citation_urls": len(distinct_urls),
        "search_queries": search_queries,
        "attachments": attachments,
        "canvas_messages": canvases,
    }


def corpus_shape(conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
    months: Counter = Counter()
    total_nodes = 0
    message_nodes = 0
    on_path = 0
    stamps: List[float] = []
    for conversation in conversations:
        mapping = conversation.get("mapping") or {}
        total_nodes += len(mapping)
        path = set(active_path_ids(conversation) or [])
        for node_id, node in mapping.items():
            if isinstance(node, dict) and isinstance(node.get("message"), dict):
                message_nodes += 1
                if node_id in path:
                    on_path += 1
        created, last_active = conversation_activity(conversation)
        stamp = last_active if last_active is not None else created
        if stamp is not None:
            stamps.append(stamp)
            months[datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m")] += 1
    return {
        "conversations": len(conversations),
        "mapping_nodes": total_nodes,
        "message_nodes": message_nodes,
        "message_nodes_on_active_path": on_path,
        "oldest_last_activity": datetime.fromtimestamp(min(stamps), tz=timezone.utc).isoformat() if stamps else None,
        "newest_last_activity": datetime.fromtimestamp(max(stamps), tz=timezone.utc).isoformat() if stamps else None,
        "conversations_by_last_activity_month": dict(sorted(months.items())),
    }


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    shape = report["corpus"]
    lines.append(f"ChatGPT export — {report['source']}")
    lines.append("")
    lines.append(
        f"  {shape['conversations']} conversations · {shape['message_nodes']} message nodes "
        f"({shape['message_nodes_on_active_path']} on the active path)"
    )
    lines.append(f"  last activity {shape['oldest_last_activity']} → {shape['newest_last_activity']}")
    window = report.get("window") or {}
    if window.get("date_from") or window.get("date_to"):
        lines.append(f"  window: {window.get('date_from_iso')} → {window.get('date_to_iso')}")
    lines.append("")
    lines.append("  conversations by month of last activity (the inclusion key)")
    for month, count in shape["conversations_by_last_activity_month"].items():
        lines.append(f"    {month}  {'█' * min(count, 48)} {count}")
    lines.append("")

    for stats in report["extractions"]:
        lines.append(f"  [{stats['label']}]")
        lines.append(
            f"    records {stats['records']}   empty {stats['empty_content_records']}"
            f"   near-duplicates {stats['duplicate_records']}"
        )
        roles = ", ".join(f"{role}={count}" for role, count in stats["by_role"].items())
        lines.append(f"    roles: {roles or '—'}")
        types = ", ".join(f"{ct}={count}" for ct, count in stats["by_content_type"].items())
        lines.append(f"    content types: {types or '—'}")
        chars = ", ".join(f"{role}={count:,}" for role, count in stats["chars_by_role"].items())
        lines.append(f"    chars: {chars or '—'}")
        ledger = stats.get("ledger")
        if ledger:
            lines.append(
                f"    kept {ledger['conversations_kept']}/{ledger['conversations_seen']} conversations"
                + (
                    f" (skipped {ledger['dropped_conversations_total']}: "
                    + ", ".join(f"{r} {c}" for r, c in ledger["dropped_conversations"].items())
                    + ")"
                    if ledger["dropped_conversations_total"]
                    else ""
                )
            )
            lines.append(
                f"    of {ledger['message_nodes']} message nodes in those, kept {ledger['turns_emitted']}"
                f" and dropped {ledger['dropped_nodes_total']}"
            )
            for reason, count in ledger["dropped_nodes"].items():
                lines.append(f"      - {reason}: {count}")
        lines.append("")

    coverage = report["declared_coverage"]
    lines.append("  declared columns (present → read by v3?)")
    for entry in coverage["conversation_columns"] + coverage["message_metadata_columns"]:
        if not entry["present_on"]:
            continue
        mark = "read" if entry["read_by_v3"] else "UNREAD"
        lines.append(f"    {entry['column']:<26} {entry['present_on']:>6}   {mark}")
    lines.append("")
    lines.append(
        f"    {coverage['distinct_citation_urls']} distinct cited URLs · "
        f"{coverage['search_queries']} search queries · "
        f"{coverage['attachments']} attachments · "
        f"{coverage['canvas_messages']} canvas messages"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="conversations.json, export folder, or export .zip")
    parser.add_argument("--months", type=float, default=None, help="keep conversations active in the last N months")
    parser.add_argument("--date-from", default=None, help="ISO date lower bound (overrides --months)")
    parser.add_argument("--date-to", default=None, help="ISO date upper bound")
    parser.add_argument("--compare", action="store_true", help="also run the legacy flattener")
    parser.add_argument("--include-alternate-branches", action="store_true")
    parser.add_argument("--include-tool-output", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()

    payload, source = resolve_export(Path(args.input).expanduser())
    conversations = [c for c in (payload if isinstance(payload, list) else [payload]) if is_conversation(c)]

    date_from = args.date_from
    if date_from is None and args.months:
        date_from = (datetime.now(tz=timezone.utc) - timedelta(days=args.months * 30.44)).isoformat()
    options = ExportOptions.from_payload(
        {
            "date_from": date_from,
            "date_to": args.date_to,
            "include_alternate_branches": args.include_alternate_branches,
            "include_tool_output": args.include_tool_output,
        }
    )

    ledger = DropLedger()
    v3 = list(iter_export(conversations, options, ledger))
    extractions = [summarise("v3 chatgpt_export", v3, ledger).to_dict()]
    if args.compare:
        extractions.insert(0, summarise("legacy flattener", legacy_records(conversations), None).to_dict())

    report = {
        "source": source,
        "corpus": corpus_shape(conversations),
        "window": {
            "date_from": options.date_from,
            "date_to": options.date_to,
            "date_from_iso": datetime.fromtimestamp(options.date_from, tz=timezone.utc).date().isoformat()
            if options.date_from
            else None,
            "date_to_iso": datetime.fromtimestamp(options.date_to, tz=timezone.utc).date().isoformat()
            if options.date_to
            else None,
        },
        "extractions": extractions,
        "declared_coverage": declared_coverage(conversations),
    }

    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
