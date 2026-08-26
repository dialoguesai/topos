"""The `kind` → kernel registry: the half of L5-22 that makes a declaration runnable.

`synthesis[]` has carried 55 lens declarations across 25 packs since the catalog was drafted.
Three had an implementation. **Zero had a dispatcher** — the only caller was a pilot script
that hardcoded two of them by name. So the ontology could say what to derive from accumulated
structure, and nothing could execute it.

This is that dispatcher, and its shape is the pack contract's own rule applied to maths: a
pack NAMES a kernel, it never ships one. Packs carry no code, exactly as they carry no prompt
— the engine owns the implementation and the pack owns the declaration. That property is what
makes opening the format to third parties mean "accept YAML" rather than "accept someone's
algorithm over your message history".

Registering a kernel is deliberately a decision, not a side effect: an unknown `kind` fails at
LOAD (`packs._load_lenses`) rather than silently at dispatch, so a lens with no implementation
is a validation error someone sees rather than a quiet no-op nobody does.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional

#: kind -> (callable, version). The version is per-KERNEL, not per-pack, because the two
#: change for different reasons: a pack version bump means the ontology changed and triggers
#: re-extraction, while a kernel bump means the maths changed and should trigger
#: re-COMPUTATION. Conflating them re-runs an LLM over a corpus to fix an arithmetic bug.
_KERNELS: Dict[str, tuple] = {}


class KernelResult:
    """What one lens produced, and enough about how to make it reviewable."""

    __slots__ = ("kind", "pack", "predicate", "rows", "abstained", "reason", "kernel_version")

    def __init__(self, kind: str, pack: str, predicate: str, rows: Optional[List] = None,
                 abstained: bool = False, reason: str = "", kernel_version: str = "") -> None:
        self.kind = kind
        self.pack = pack
        self.predicate = predicate
        self.rows = rows or []
        self.abstained = abstained
        self.reason = reason
        self.kernel_version = kernel_version

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        state = f"abstained:{self.reason}" if self.abstained else f"{len(self.rows)} rows"
        return f"KernelResult({self.pack}/{self.kind}, {state})"


def register_kernel(kind: str, version: str = "1") -> Callable:
    """Decorator. A kernel takes (conn, pack, lens, owner) and returns a list of rows."""
    def _wrap(fn: Callable) -> Callable:
        if kind in _KERNELS:
            raise ValueError(f"kernel {kind!r} is already registered")
        _KERNELS[kind] = (fn, version)
        return fn
    return _wrap


def registered_kinds() -> set:
    return set(_KERNELS)


def kernel_version(kind: str) -> Optional[str]:
    entry = _KERNELS.get(kind)
    return entry[1] if entry else None


def unimplemented_kinds(packs: Dict[str, Any]) -> Dict[str, int]:
    """Declared kinds with no kernel, and how many declarations wait on each.

    Visibility, not an error: the gap between what the ontology asks for and what the engine
    can compute is the actual size of the remaining work, and it should be a number rather
    than a feeling.
    """
    out: Dict[str, int] = {}
    for p in packs.values():
        for lens in getattr(p, "lenses", []):
            if lens.kind not in _KERNELS:
                out[lens.kind] = out.get(lens.kind, 0) + 1
    return out


def run_lens(conn: sqlite3.Connection, pack: Any, lens: Any, owner: str) -> KernelResult:
    """Dispatch one lens to its kernel.

    Abstention is a RESULT, never an exception. A lens below its evidence floor has produced
    a legitimate answer — "not enough to say" — and turning that into an error would make
    every honest abstention look like a broken pipeline.
    """
    entry = _KERNELS.get(lens.kind)
    if entry is None:
        return KernelResult(lens.kind, pack.pack, lens.predicate, abstained=True,
                            reason="no_kernel_registered")
    fn, version = entry
    if not owner:
        return KernelResult(lens.kind, pack.pack, lens.predicate, abstained=True,
                            reason="no_owner_entity", kernel_version=version)
    try:
        rows = fn(conn, pack, lens, owner)
    except Exception as exc:  # noqa: BLE001 — one bad kernel must not stop the pass
        return KernelResult(lens.kind, pack.pack, lens.predicate, abstained=True,
                            reason=f"kernel_error:{str(exc)[:80]}", kernel_version=version)
    if rows is None:
        # None is a kernel that could not answer, not an empty answer. Coercing it to []
        # made abstention unreachable from inside a kernel: the one honest signal it could
        # send was being rewritten into "computed successfully, found nothing".
        return KernelResult(lens.kind, pack.pack, lens.predicate, abstained=True,
                            reason="kernel_returned_none", kernel_version=version)
    return KernelResult(lens.kind, pack.pack, lens.predicate, rows=rows,
                        kernel_version=version)


def run_pack_lenses(conn: sqlite3.Connection, pack: Any, owner: str) -> List[KernelResult]:
    return [run_lens(conn, pack, lens, owner) for lens in getattr(pack, "lenses", [])]


# --------------------------------------------------------------------------- kernels
#
# The three that already had implementations, now reachable by declaration rather than by a
# pilot script that named them. They delegate to `synthesize.py` so the maths has one home.

@register_kernel("rhythm", version="1")
def _rhythm(conn, pack, lens, owner):
    from .synthesize import synthesize_rhythm
    from .writer import DerivationWriter

    return synthesize_rhythm(conn, DerivationWriter(conn, model="lens:rhythm"), pack, owner)


@register_kernel("graph_labeling", version="1")
def _graph_labeling(conn, pack, lens, owner):
    from .synthesize import synthesize_closeness
    from .writer import DerivationWriter

    return synthesize_closeness(conn, DerivationWriter(conn, model="lens:graph_labeling"),
                                pack, owner)


@register_kernel("trajectory", version="1")
def _trajectory(conn, pack, lens, owner):
    from .synthesize import synthesize_trajectory
    from .writer import DerivationWriter

    return synthesize_trajectory(conn, DerivationWriter(conn, model="lens:trajectory"),
                                 pack, owner)


def load_all_kernels() -> set:
    """Import every kernel module so registration has happened.

    Registration is an import side effect, which means a kernel nobody imported is a kernel
    that silently does not exist. Calling this before dispatch turns that into a fact the
    caller can check rather than an absence they discover at runtime.
    """
    from . import social_kernels  # noqa: F401 — imported for its @register_kernel calls

    return registered_kinds()
