"""Keep the models volume above the owner's floor by evicting what we can re-pull.

`disk_space` answers "is there room?". This module is what happens when the
answer is no and the node still needs a model to do the work it was asked to do.

The asymmetry that shapes it: an Ollama model is the only large thing on this
volume that is *reproducible*. Deleting one costs a download; deleting anything
else costs data. So when the floor is breached, models are what gets removed —
and only the ones nothing is currently pointed at.

Three rules, in order, and none of them is negotiable:

  1. **Never evict a model something is bound to.** The active pack's roles and
     every node-function config (signal extraction, facts, conversation context,
     community naming, sanitization) name models this node runs to answer the
     owner. Removing one turns a working node into a 404 on the next request,
     which is a worse outcome than a full disk the owner can see.
  2. **Never evict the model we are making room for.** Freeing space for a pull
     by deleting the thing being pulled is a loop, not a plan.
  3. **Evict least-recently-used first.** Ollama's `/api/tags` gives
     `modified_at`, which is the only recency signal available here. It is the
     time the tag was written, not last used — so it is a proxy, and the code
     says so rather than pretending otherwise. Ties break on size, largest
     first, so the fewest deletions clear the shortfall.

What is removed is recorded in `engine_config` so the owner can see what the
node did on their behalf, and so a later "put it back" knows the tag and the
size it will cost. Nothing here re-pulls automatically: a background download
the owner did not ask for is exactly the surprise this feature exists to avoid.
`ensure_model` re-pulls only when something is asking for that model *now*.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set

from .disk_space import (
    format_bytes,
    free_bytes,
    min_free_bytes,
    ollama_models_dir,
    space_check_applies,
)

logger = logging.getLogger("topos.engine.model_manager")

#: Where the record of what we removed lives. Read by the settings panel; never
#: read at decision time — the eviction plan is computed from the live tag list,
#: not from this log.
ENGINE_CONFIG_KEY_EVICTIONS = "model_manager_evictions"

#: The log is a receipt, not a history. More than this and it is being kept as
#: something it is not.
_MAX_EVICTION_RECORDS = 32


class EvictionCandidate(NamedTuple):
    """One removable model, with what removing it buys."""

    tag: str
    size_bytes: int
    modified_at: str


class ReclaimResult(NamedTuple):
    """What a reclaim actually did — the numbers a caller reports or logs."""

    removed: List[str]
    freed_bytes: int
    #: What is still missing after the removals, 0 when the target was met.
    shortfall_bytes: int
    #: True when the target was met (including "it was already met").
    satisfied: bool
    #: Set when nothing was attempted, so a caller can tell "there was nothing
    #: to remove" from "we removed things and it was not enough".
    reason: str = ""


def normalize_tag(value: Any) -> str:
    """Ollama's own spelling of a tag: a bare name means `:latest`.

    `llama3.2` and `llama3.2:latest` are one model to Ollama and two strings to
    us. Protecting one spelling while the tag list carries the other is how a
    bound model gets deleted, so every comparison in this module goes through
    here first.
    """
    tag = str(value or "").strip()
    if not tag:
        return ""
    # A digest-pinned reference (`model@sha256:…`) names the same model.
    if "@" in tag:
        tag = tag.split("@", 1)[0].strip()
    if ":" not in tag:
        return f"{tag}:latest"
    return tag


def _adapter(adapter: Any = None) -> Any:
    if adapter is not None:
        return adapter
    from .backends.ollama import OllamaAdapter

    return OllamaAdapter()


def installed_models(adapter: Any = None) -> List[Dict[str, Any]]:
    """`[{tag, size_bytes, modified_at}]` from Ollama, or `[]` when unreachable.

    An empty list is not "no models" — `list_models_detailed` swallows a dead
    daemon the same way. That is deliberate here: with no tag list there is
    nothing to evict, and the caller's next step (refuse the pull) is the same
    either way.
    """
    resolved = _adapter(adapter)
    try:
        rows = resolved.list_models_detailed()
    except Exception as exc:  # noqa: BLE001 — an unreachable daemon has nothing to evict
        logger.debug("model manager could not list models: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tag = normalize_tag(row.get("name"))
        if not tag:
            continue
        try:
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append(
            {
                "tag": tag,
                "size_bytes": max(0, size),
                "modified_at": str(row.get("modified_at") or ""),
            }
        )
    return out


def protected_tags(conn: Any = None) -> Set[str]:
    """Every local model this node is currently pointed at.

    Read from the live config rather than a cached list, because the point of
    the set is to be right at the moment of deletion. Each source is wrapped:
    one unreadable config must not silently shrink the protected set — that is
    the failure that deletes a model the node needs — so a failure logs and the
    rest of the sources still contribute.
    """
    tags: Set[str] = set()

    def _add(value: Any, provider: Any = "ollama") -> None:
        # Only local models occupy this disk. A role bound to a hosted provider
        # names a model that was never on it.
        if str(provider or "").strip().lower() not in ("", "ollama"):
            return
        tag = normalize_tag(value)
        if tag:
            tags.add(tag)

    from ..config.settings import settings

    # The engine's own fallbacks. These are what every function drops back to
    # when its override is cleared, so they are needed even when nothing names
    # them right now.
    for attr in ("ollama_query_model", "ollama_extraction_model", "facts_llm_model"):
        _add(getattr(settings, attr, ""))

    if conn is None:
        try:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        except Exception as exc:  # noqa: BLE001
            logger.debug("model manager has no database for protection: %s", exc)
            conn = None

    if conn is None:
        return tags

    def _source(name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — one bad config must not shrink the set
            logger.warning(
                "model manager could not read %s while protecting models; "
                "leaving its models unprotected is the one thing that must not "
                "happen silently: %s",
                name,
                exc,
            )

    def _pack() -> None:
        from ..config.model_packs import active_pack_dict

        pack = active_pack_dict(conn) or {}
        for binding in (pack.get("roles") or {}).values():
            if isinstance(binding, dict):
                _add(binding.get("model"), binding.get("provider"))

    def _signal() -> None:
        from ..config.signal_extraction import resolve_signal_extraction_config

        cfg = resolve_signal_extraction_config(settings, conn)
        _add(cfg.query_model, cfg.provider)

    def _facts() -> None:
        from ..config.facts_llm import resolve_facts_llm_request

        provider, model = resolve_facts_llm_request(settings, conn)
        _add(model, provider)

    def _context() -> None:
        from ..config.conversation_context_llm import resolve_context_llm_request

        provider, model = resolve_context_llm_request(settings, conn)
        _add(model, provider)

    def _naming() -> None:
        from ..features.entities.community_naming import resolve_naming_model

        _add(resolve_naming_model(conn))

    def _sanitization() -> None:
        from ..config.sanitization_ollama import resolve_sanitization_ollama_effective

        cfg = resolve_sanitization_ollama_effective(settings, conn)
        _add(getattr(cfg, "default_model", ""))
        for model in (getattr(cfg, "models", None) or {}).values():
            _add(model)

    _source("model pack", _pack)
    _source("signal extraction config", _signal)
    _source("facts LLM config", _facts)
    _source("conversation context config", _context)
    _source("community naming config", _naming)
    _source("sanitization config", _sanitization)
    return tags


def _recency_key(row: Dict[str, Any]) -> Any:
    """Sort key: oldest `modified_at` first, then largest model first.

    A tag with no timestamp sorts oldest. Ollama has always sent one; a tag
    without it is either a very old daemon or a shape we do not understand, and
    "we cannot tell how recent this is" is a better reason to consider it for
    eviction than to protect it forever.
    """
    return (str(row.get("modified_at") or ""), -int(row.get("size_bytes") or 0))


def eviction_candidates(
    *,
    conn: Any = None,
    adapter: Any = None,
    keep: Iterable[str] = (),
    models: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[EvictionCandidate]:
    """Removable models, least-recently-written first.

    `keep` is the caller's own protection — the tag it is making room for, plus
    anything it knows is in flight. It stacks with `protected_tags`; neither
    replaces the other.
    """
    rows = list(models) if models is not None else installed_models(adapter)
    spared = protected_tags(conn) | {normalize_tag(t) for t in keep if normalize_tag(t)}
    candidates = [row for row in rows if row.get("tag") not in spared]
    candidates.sort(key=_recency_key)
    return [
        EvictionCandidate(
            tag=str(row.get("tag") or ""),
            size_bytes=int(row.get("size_bytes") or 0),
            modified_at=str(row.get("modified_at") or ""),
        )
        for row in candidates
    ]


def reclaimable_bytes(
    *, conn: Any = None, adapter: Any = None, keep: Iterable[str] = ()
) -> int:
    """Everything eviction could free right now, for the settings panel."""
    return sum(c.size_bytes for c in eviction_candidates(conn=conn, adapter=adapter, keep=keep))


def _record_evictions(conn: Any, removed: Sequence[EvictionCandidate], reason: str) -> None:
    """Append what was removed to the receipt in engine_config.

    Best-effort by design: a failed write here must never turn a successful
    reclaim into an error. The models are already gone; losing the note about it
    is the smaller loss.
    """
    if conn is None or not removed:
        return
    try:
        from ..core.state import get_engine_config_value, set_engine_config_value

        try:
            existing = json.loads(get_engine_config_value(conn, ENGINE_CONFIG_KEY_EVICTIONS) or "[]")
        except Exception:  # noqa: BLE001 — a corrupt receipt starts over
            existing = []
        if not isinstance(existing, list):
            existing = []
        now = time.time()
        for candidate in removed:
            existing.append(
                {
                    "tag": candidate.tag,
                    "size_bytes": candidate.size_bytes,
                    "evicted_at": now,
                    "reason": reason,
                }
            )
        set_engine_config_value(
            conn,
            ENGINE_CONFIG_KEY_EVICTIONS,
            json.dumps(existing[-_MAX_EVICTION_RECORDS:]),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not record model evictions: %s", exc)


def recent_evictions(conn: Any = None) -> List[Dict[str, Any]]:
    """The receipt, newest last. `[]` when nothing has been removed."""
    if conn is None:
        try:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        except Exception:  # noqa: BLE001
            return []
    if conn is None:
        return []
    try:
        from ..core.state import get_engine_config_value

        rows = json.loads(get_engine_config_value(conn, ENGINE_CONFIG_KEY_EVICTIONS) or "[]")
    except Exception:  # noqa: BLE001
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def reclaim_for(
    needed_bytes: Any = 0,
    *,
    conn: Any = None,
    adapter: Any = None,
    keep: Iterable[str] = (),
    base_url: Any = None,
    reason: str = "disk_floor",
) -> ReclaimResult:
    """Delete re-downloadable models until `needed_bytes` fits above the floor.

    The target is `needed_bytes + the owner's floor`. Passing 0 means "just get
    back above the floor", which is what a routine sweep wants.

    Deletes one model at a time and re-checks against the volume rather than
    against arithmetic: Ollama shares blobs between tags, so removing a 4 GB tag
    can free 4 GB, or almost nothing if another tag holds the same layers.
    Trusting the subtraction would delete more models than the shortfall needed.
    """
    if not space_check_applies(base_url):
        # Not our disk. Nothing here is ours to delete either.
        return ReclaimResult([], 0, 0, True, reason="remote_ollama")

    if conn is None:
        try:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        except Exception:  # noqa: BLE001
            conn = None

    try:
        needed = max(0, int(needed_bytes or 0))
    except (TypeError, ValueError):
        needed = 0
    target = needed + min_free_bytes(conn)

    path = ollama_models_dir()
    available = free_bytes(path)
    if available is None:
        # An unreadable volume is not a full one — the same rule the space check
        # follows. Deleting models on a guess is not recoverable.
        return ReclaimResult([], 0, 0, True, reason="volume_unreadable")
    if available >= target:
        return ReclaimResult([], 0, 0, True, reason="already_above_floor")

    resolved = _adapter(adapter)
    candidates = eviction_candidates(conn=conn, adapter=resolved, keep=keep)
    if not candidates:
        return ReclaimResult(
            [], 0, max(0, target - available), False, reason="nothing_evictable"
        )

    started_with = available
    removed: List[EvictionCandidate] = []
    for candidate in candidates:
        if available >= target:
            break
        try:
            resolved.delete_model(candidate.tag)
        except Exception as exc:  # noqa: BLE001 — a tag that will not delete is not fatal
            logger.warning("model manager could not remove %s: %s", candidate.tag, exc)
            continue
        removed.append(candidate)
        logger.info(
            "model manager removed %s (%s) to stay above the %s disk floor",
            candidate.tag,
            format_bytes(candidate.size_bytes),
            format_bytes(min_free_bytes(conn)),
        )
        probed = free_bytes(path)
        if probed is not None:
            available = probed

    _record_evictions(conn, removed, reason)
    freed = max(0, available - started_with)
    satisfied = available >= target
    return ReclaimResult(
        removed=[c.tag for c in removed],
        freed_bytes=freed,
        shortfall_bytes=0 if satisfied else max(0, target - available),
        satisfied=satisfied,
        reason="" if satisfied else "still_short",
    )


def ensure_model(
    model: str,
    *,
    size_bytes: Any = None,
    conn: Any = None,
    adapter: Any = None,
) -> Dict[str, Any]:
    """Make `model` available, freeing space for it first if the floor requires.

    This is the "switch between models by removing and re-downloading" path: a
    model that is already installed answers immediately, and one that is not is
    pulled after whatever eviction the floor demands. The pull itself goes
    through `ollama_pull.start_pull`, so a caller polls progress exactly as the
    setup card already does.
    """
    tag = normalize_tag(model)
    if not tag:
        raise ValueError("model is required")

    resolved = _adapter(adapter)
    if any(row.get("tag") == tag for row in installed_models(resolved)):
        return {"model": tag, "state": "present", "removed": []}

    reclaim = reclaim_for(size_bytes, conn=conn, adapter=resolved, keep=[tag])

    from .ollama_pull import start_pull

    record = start_pull(tag, adapter=resolved, known_size_bytes=size_bytes)
    return {**record, "removed": list(reclaim.removed), "freed_bytes": reclaim.freed_bytes}


def status(conn: Any = None, *, adapter: Any = None, base_url: Any = None) -> Dict[str, Any]:
    """Disk floor plus what the model manager could do about it.

    One payload because the settings panel and the sidebar warning ask the same
    question — "are we under the floor, and is there anything to be done?" — and
    two endpoints would let the two surfaces disagree.
    """
    from .disk_space import disk_status

    payload = disk_status(conn, base_url=base_url)
    if conn is None:
        try:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        except Exception:  # noqa: BLE001
            conn = None

    models = installed_models(adapter) if payload.get("applies") else []
    candidates = (
        eviction_candidates(conn=conn, adapter=adapter, models=models) if models else []
    )
    payload.update(
        {
            "installed_model_count": len(models),
            "installed_model_bytes": sum(int(r.get("size_bytes") or 0) for r in models),
            "reclaimable_bytes": sum(c.size_bytes for c in candidates),
            "reclaimable_model_count": len(candidates),
            "protected_model_count": max(0, len(models) - len(candidates)),
            "recent_evictions": recent_evictions(conn)[-8:],
        }
    )
    return payload
