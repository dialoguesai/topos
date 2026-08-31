#!/usr/bin/env python3
"""Pack a shadow-lab snapshot into the explorer page.

Takes the snapshot written by ``chatgpt_shadow_lab.py`` (plus the shadow
database it came from, for the joins the snapshot does not carry) and produces
the viewer payload, injected into a copy of the explorer template.

    python scripts/chatgpt_shadow_pack.py \\
        --snapshot ~/.topos/shadow/chatgpt-before-rules-snapshot.json \\
        --db ~/.topos/shadow/chatgpt-before-rules.db \\
        --export ../conversations.json \\
        --template kg-explorer.html --out kg-explorer-before.html

The payload holds the owner's own material. Write it outside the repo.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLACEHOLDER = "__SNAPSHOT_JSON__"

# Viewer caps. The page shows full counts from ``counts`` regardless; these only
# bound what it can page through, and every one that bites is reported on the page.
VIEW_CAPS = {"entity_mentions": 9000, "facts": 3000, "signal_objects": 1500}

# What the export declares, is already read into message metadata, and is not yet
# a node the graph can reach. Sourced from the import report so the page cannot
# drift from the measurement.
GAP_LABELS = {
    "distinct_citation_urls": "Cited & search-result URLs",
    "search_queries": "Search queries run for you",
    "canvas_messages": "Canvas documents",
    "attachments": "Attachments",
}


def declared_gaps(export_path: Path) -> List[Dict[str, Any]]:
    from scripts.chatgpt_import_report import declared_coverage, resolve_export  # noqa: E402

    payload, _ = resolve_export(export_path)
    conversations = payload if isinstance(payload, list) else [payload]
    coverage = declared_coverage([c for c in conversations if isinstance(c, dict)])
    return [
        {"label": label, "count": int(coverage.get(key) or 0), "status": "captured, not minted"}
        for key, label in GAP_LABELS.items()
        if coverage.get(key)
    ]


def conversation_for_records(conn: sqlite3.Connection) -> Dict[str, str]:
    """message_id → conversation_id, so a mention can name where it came from."""
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT message_id, conversation_id FROM ai_chat_messages")}
    except sqlite3.Error:
        return {}


def pack(
    snapshot: Dict[str, Any],
    conn: sqlite3.Connection,
    export: Path | None,
    previous: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    record_to_conv = conversation_for_records(conn)
    titles = {
        row[0]: row[1]
        for row in conn.execute("SELECT conversation_id, title FROM ai_chat_conversations")
    }

    mentions = snapshot.get("entity_mentions", [])[: VIEW_CAPS["entity_mentions"]]
    for mention in mentions:
        conv = record_to_conv.get(mention.get("record_id"))
        if conv:
            mention["conversation_id"] = conv
            mention["conversation_title"] = titles.get(conv)
        # Surface text can be a whole paragraph; the viewer only needs the phrase.
        text = mention.get("surface_text")
        if isinstance(text, str) and len(text) > 180:
            mention["surface_text"] = text[:180] + "…"

    facts = []
    for fact in snapshot.get("facts", [])[: VIEW_CAPS["facts"]]:
        fact.pop("payload", None)  # the rendered text is what the page shows
        facts.append(fact)

    objects = []
    for obj in snapshot.get("signal_objects", [])[: VIEW_CAPS["signal_objects"]]:
        obj.pop("payload", None)
        objects.append(obj)

    for entity in snapshot.get("entities", []):
        entity.pop("metadata", None)
    for edge in snapshot.get("entity_edges", []):
        edge.pop("metadata", None)

    meta = dict(snapshot.get("meta") or {})
    if previous is not None:
        meta["previous"] = {
            "label": (previous.get("meta") or {}).get("label") or "previous run",
            "counts": previous.get("counts") or {},
        }
    if export is not None:
        try:
            meta["declared_gaps"] = declared_gaps(export)
        except Exception as exc:  # noqa: BLE001 — the page is still useful without them
            print(f"warning: could not read declared gaps ({exc})", file=sys.stderr)

    view_capped = dict(meta.get("capped_tables") or {})
    for name, cap in VIEW_CAPS.items():
        total = int((snapshot.get("counts") or {}).get(name, 0) or 0)
        if total > cap:
            view_capped[name] = total
    meta["capped_tables"] = view_capped

    return {
        "meta": meta,
        "counts": snapshot.get("counts", {}),
        "conversations": snapshot.get("conversations", []),
        "entities": snapshot.get("entities", []),
        "entity_edges": snapshot.get("entity_edges", []),
        "entity_mentions": mentions,
        "facts": facts,
        "signal_objects": objects,
        "topic_clusters": snapshot.get("topic_clusters", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--export", default=None, help="the export, for the declared-gap bars")
    parser.add_argument(
        "--previous",
        default=None,
        help="an earlier snapshot; its counts become the before half of every tile",
    )
    parser.add_argument("--template", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    snapshot = json.loads(Path(args.snapshot).expanduser().read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{Path(args.db).expanduser()}?mode=ro", uri=True)
    try:
        previous = (
            json.loads(Path(args.previous).expanduser().read_text(encoding="utf-8"))
            if args.previous
            else None
        )
        payload = pack(
            snapshot, conn, Path(args.export).expanduser() if args.export else None, previous
        )
    finally:
        conn.close()

    template = Path(args.template).expanduser().read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise SystemExit(f"{args.template} has no {PLACEHOLDER} placeholder")
    # </script> inside the JSON would close the host tag early.
    encoded = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    out = Path(args.out).expanduser()
    out.write_text(template.replace(PLACEHOLDER, encoded), encoding="utf-8")

    size_mb = out.stat().st_size / 1024**2
    counts = payload["counts"]
    print(f"wrote {out}  ({size_mb:.1f} MB)")
    print(f"  entities {counts.get('entities')} · edges {counts.get('entity_edges')} ·"
          f" mentions {counts.get('entity_mentions')} · facts {counts.get('facts')}")
    if payload["meta"].get("capped_tables"):
        print(f"  capped for the viewer: {payload['meta']['capped_tables']}")
    if size_mb > 14:
        print("  WARNING: over 14MB — the artifact ceiling is 16MB; tighten VIEW_CAPS.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
