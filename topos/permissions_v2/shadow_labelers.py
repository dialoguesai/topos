"""Where a node's second labeler is registered, and what happens when it has none (C6).

Which model a node re-scores with is the owner's choice and a deployment fact. This module is the seam, not the
choice: a node registers a labeler and the shadow audit can score; a node that has not answers `unresolved` /
`labeler_unavailable` for every sample and the owner sees a hole with a name.

**Nothing is registered by default, and that is the safe direction.** The alternative -- some default that answers
`agree` when nothing objects -- would manufacture evidence, because the report card's output is a count of items
that were checked and an item nobody checked must not be in it.

A labeler is anything with:

    id      a stable name for the row (`local-qwen3.5-9b`)
    family  the model family, so a second opinion from the family that gave the first is refused as `same_family`
    score(records) -> "agree" | "candidate_miss" | "unresolved"

`records` are the released records as the node resolved them, with content. The labeler returns a verdict and
nothing else: no rationale, no scores, no text. What it may NOT do is conclude a miss -- only the owner does that,
on the control plane, after the flag.
"""
from __future__ import annotations

import threading

_registry: dict = {}
_lock = threading.Lock()
MODES = ("local", "hosted")


def register(mode: str, labeler) -> None:
    """Bind a labeler for `local` or `hosted`. The hosted one is reached only after the control plane has
    checked the owner's standing consent; registering it does not grant that consent."""
    if mode not in MODES:
        raise ValueError("unknown labeler mode")
    for attribute in ("id", "family", "score"):
        if not hasattr(labeler, attribute):
            raise ValueError("a labeler needs id, family and score")
    with _lock:
        _registry[mode] = labeler


def unregister(mode: str) -> None:
    with _lock:
        _registry.pop(mode, None)


def registered(mode: str):
    with _lock:
        return _registry.get(mode)


def clear() -> None:
    with _lock:
        _registry.clear()
