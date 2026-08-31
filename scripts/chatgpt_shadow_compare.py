#!/usr/bin/env python3
"""Diff two shadow-lab snapshots: same export, different pipeline.

    python scripts/chatgpt_shadow_compare.py \\
        --before ~/.topos/shadow/chatgpt-before-snapshot.json \\
        --after  ~/.topos/shadow/chatgpt-after-snapshot.json

Prints what changed in the graph, and — because a bigger number is not the same
as a better graph — what changed in its *shape*: how many entities are actually
joined to something, and how many were minted from a declared column rather than
guessed by a model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

COUNT_ROWS = [
    ("messages", "turns stored"),
    ("owner_turns", "turns you wrote"),
    ("entities", "entities"),
    ("entity_edges", "entity edges"),
    ("entity_mentions", "mentions"),
    ("facts", "facts"),
    ("signal_objects", "signal objects"),
    ("message_topics", "topic tags"),
    ("embeddings", "embeddings"),
    ("graph_nodes", "graph nodes"),
    ("graph_edges", "graph edges"),
]


def load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def shape(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Structural readings a raw count cannot give."""
    entities = snapshot.get("entities") or []
    edges = snapshot.get("entity_edges") or []
    connected = set()
    for edge in edges:
        connected.add(edge.get("src_entity_id"))
        connected.add(edge.get("dst_entity_id"))
    by_type: Dict[str, int] = {}
    for entity in entities:
        key = str(entity.get("entity_type") or "unknown")
        by_type[key] = by_type.get(key, 0) + 1
    edge_types: Dict[str, int] = {}
    for edge in edges:
        key = str(edge.get("edge_type") or "unknown")
        edge_types[key] = edge_types.get(key, 0) + 1
    joined = sum(1 for e in entities if e.get("entity_id") in connected)
    return {
        "entities_in_snapshot": len(entities),
        "entities_with_an_edge": joined,
        "isolated_entities": len(entities) - joined,
        "mean_degree": round((2 * len(edges)) / len(entities), 2) if entities else 0.0,
        "entity_types": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "edge_types": dict(sorted(edge_types.items(), key=lambda kv: -kv[1])),
    }


def delta_rows(before: Dict[str, Any], after: Dict[str, Any]) -> List[Tuple[str, int, int]]:
    bc, ac = before.get("counts") or {}, after.get("counts") or {}
    out = []
    for key, label in COUNT_ROWS:
        out.append((label, int(bc.get(key) or 0), int(ac.get(key) or 0)))
    return out


def render(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    bm, am = before.get("meta") or {}, after.get("meta") or {}
    lines = [
        f"before  {bm.get('label')}  ({bm.get('captured_at', '')[:19]})",
        f"after   {am.get('label')}  ({am.get('captured_at', '')[:19]})",
        "",
        f"  {'':32} {'before':>10} {'after':>10} {'delta':>10}",
    ]
    for label, b, a in delta_rows(before, after):
        diff = a - b
        mark = "" if diff == 0 else ("  +" + f"{diff:,}" if diff > 0 else f"  {diff:,}")
        lines.append(f"  {label:32} {b:>10,} {a:>10,} {mark:>10}")

    bs, as_ = shape(before), shape(after)
    lines += ["", "  graph shape"]
    for key in ("entities_with_an_edge", "isolated_entities", "mean_degree"):
        lines.append(f"    {key:30} {bs[key]:>10} {as_[key]:>10}")

    lines += ["", "  entity types"]
    for key in sorted(set(bs["entity_types"]) | set(as_["entity_types"]),
                      key=lambda k: -as_["entity_types"].get(k, 0)):
        b, a = bs["entity_types"].get(key, 0), as_["entity_types"].get(key, 0)
        lines.append(f"    {key:30} {b:>10,} {a:>10,}{'   NEW' if b == 0 and a else ''}")

    lines += ["", "  edge types"]
    for key in sorted(set(bs["edge_types"]) | set(as_["edge_types"]),
                      key=lambda k: -as_["edge_types"].get(k, 0)):
        b, a = bs["edge_types"].get(key, 0), as_["edge_types"].get(key, 0)
        lines.append(f"    {key:30} {b:>10,} {a:>10,}{'   NEW' if b == 0 and a else ''}")

    bd, ad = bm.get("declared_coverage") or {}, am.get("declared_coverage") or {}
    if bd or ad:
        lines += ["", "  declared lane (what the export hands us)"]
        for key in sorted(set(bd) | set(ad)):
            lines.append(f"    {key:30} {str(bd.get(key, '—')):>10} {str(ad.get(key, '—')):>10}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    before = load(Path(args.before).expanduser())
    after = load(Path(args.after).expanduser())
    if args.json:
        print(json.dumps({
            "before": {"meta": before.get("meta"), "counts": before.get("counts"), "shape": shape(before)},
            "after": {"meta": after.get("meta"), "counts": after.get("counts"), "shape": shape(after)},
        }, indent=2, default=str))
    else:
        print(render(before, after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
