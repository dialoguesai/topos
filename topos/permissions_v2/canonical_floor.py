"""External rollback floor for the canonical permission state.

The protection clock is monotone *inside* the canonical database. It cannot see
the database being replaced by an older copy of itself, which is the one move
that turns a revoked attestation back into a live one and an emptied event log
back into a clean history. This floor lives outside that file and records what
the canonical state had reached.

Three different rules, because the three things it pins move differently:

* the **attestation ledger** is pinned exactly. Only the attestation handler
  appends to it, and only the handler republishes this floor for it. A ledger
  row that appears any other way is therefore a tamper, and every read fails
  closed rather than adopting it.
* the **event log** and the **restriction registry** grow from the node's own
  native writes, which have no business publishing a consent floor. They are
  pinned as prefixes: a read adopts an extension and republishes, and refuses
  anything that is not an extension of what the floor already saw.
* the **generation** only ever advances. A lower one is a restore.

Published pending before the canonical commit and active after it, so a crash
in either order leaves the floor closed rather than silently ahead of the data.
It is not an integrity boundary against a privileged host administrator who
rewrites every trusted file together; it is what makes the ordinary, plausible
restore of one file visible.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sqlite3
from typing import Literal

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Generation, Hash, Identifier, Number, StrictModel
from .protection_clock import EVENTS, LEDGER, REGISTRY, TABLE, clock_state

FLOOR_VERSION = "topos-permissions-canonical-floor/v1"
CHAIN_SEED = digest({"version": FLOOR_VERSION, "chain": "event-log/v1"})
MAX_FLOOR_BYTES = 8192


class CanonicalFloor(StrictModel):
    """What the canonical permission state had reached, recorded outside it."""
    version: Literal["topos-permissions-canonical-floor/v1"]
    state: Literal["pending", "active"]
    owner_id: Identifier
    node_id: Identifier
    resource_id: Identifier
    clock_id: Hash
    generation: Number
    event_sequence: Number
    event_chain: Hash
    ledger_sequence: Number
    ledger_digest: Hash
    registry_count: Number
    registry_digest: Hash
    revision: Generation


def _fold(chain: str, rows) -> str:
    """One step per event, in sequence order. Order is part of what is pinned."""
    for sequence, generation, source, artifact_key in rows:
        chain = digest({"chain": chain, "sequence": sequence, "generation": generation,
                        "source": source, "artifact_key": artifact_key})
    return chain


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        raise PolicyError("canonical_floor_unavailable") from None


def event_chain(conn, *, through: int | None = None, start: tuple[int, str] | None = None) -> tuple[int, str]:
    """Fold the event log, optionally resuming from a prefix already folded.

    `start` is `(sequence, chain)` from an earlier fold of the same log. Only
    rows after that sequence are read, which is what makes this cheap on a node
    whose log grows all day. Resuming is safe because the log is append-only by
    trigger: the rows before that point cannot have changed without the table
    itself being replaced, which the sequence and generation checks catch.
    """
    sequence, chain = start if start else (0, CHAIN_SEED)
    clause = "" if through is None else f" AND sequence<={int(through)}"
    rows = _rows(conn, f"SELECT sequence,generation,source,artifact_key FROM {EVENTS} "
                       f"WHERE sequence>?{clause} ORDER BY sequence", (sequence,))
    for row in rows:
        if row[0] <= sequence:
            raise PolicyError("canonical_floor_unavailable")
        sequence = row[0]
    return sequence, _fold(chain, rows)


def ledger_state(conn) -> tuple[int, str]:
    """Every consent row, exactly. Never adopted by a read."""
    rows = _rows(conn, f"SELECT sequence,entry_id,action,entity_id,target_entry_id,entity_type,is_self,"
                       f"contact_id,composition_revision,statement_version,command_id,command_hash,generation "
                       f"FROM {LEDGER} ORDER BY sequence")
    last = rows[-1][0] if rows else 0
    return last, digest({"version": "owner-identity-ledger/v1", "rows": [list(row) for row in rows]})


def registry_state(conn) -> tuple[int, str]:
    """Owner spellings, as a prefix: the registry grows from native writes."""
    rows = _rows(conn, f"SELECT entity_id,basis,first_generation FROM {REGISTRY} ORDER BY entity_id")
    return len(rows), digest({"version": "owner-identity-registry/v1", "rows": [list(row) for row in rows]})


def observe(conn, *, owner_id: str, node_id: str, resource_id: str, revision: int,
            state: str = "active", resume: tuple[int, str] | None = None) -> CanonicalFloor:
    """Read the canonical permission state as a floor body. Never writes."""
    clock_id, generation = clock_state(conn)
    sequence, chain = event_chain(conn, start=resume)
    ledger_sequence, ledger = ledger_state(conn)
    count, registry = registry_state(conn)
    return CanonicalFloor(version=FLOOR_VERSION, state=state, owner_id=owner_id, node_id=node_id,
        resource_id=resource_id, clock_id=clock_id, generation=generation, event_sequence=sequence,
        event_chain=chain, ledger_sequence=ledger_sequence, ledger_digest=ledger, registry_count=count,
        registry_digest=registry, revision=revision)


class CanonicalFloorStore:
    """The floor file, and the only rules by which it may move."""

    def __init__(self, path: Path, *, owner_id: str, node_id: str, resource_id: str):
        self.path = Path(path)
        self.owner_id, self.node_id, self.resource_id = owner_id, node_id, resource_id
        self._current: CanonicalFloor | None = None
        # An in-process fold of the append-only log, extended rather than redone.
        self._resume: tuple[int, str] | None = None

    # --- file handling ------------------------------------------------------

    def _write(self, body: CanonicalFloor) -> None:
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(body.model_dump()))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            parent = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError:
            raise PolicyError("canonical_floor_unavailable") from None

    def _load(self) -> CanonicalFloor:
        try:
            info = os.lstat(self.path)
            if not os.path.isfile(self.path) or info.st_size > MAX_FLOOR_BYTES or info.st_uid != os.getuid():
                raise PolicyError("canonical_floor_unavailable")
            body = CanonicalFloor.parse(self.path.read_bytes())
        except (OSError, ValueError):
            raise PolicyError("canonical_floor_unavailable") from None
        if (body.owner_id != self.owner_id or body.node_id != self.node_id
            or body.resource_id != self.resource_id):
            raise PolicyError("canonical_floor_unavailable")
        if self._current is not None and body.revision < self._current.revision:
            # The file went backwards under a running process.
            raise PolicyError("canonical_floor_rollback")
        return body

    # --- the rules -----------------------------------------------------------

    def _compare(self, floor: CanonicalFloor, observed: CanonicalFloor) -> None:
        if observed.clock_id != floor.clock_id:
            raise PolicyError("canonical_floor_rollback")
        if observed.generation < floor.generation or observed.event_sequence < floor.event_sequence:
            raise PolicyError("canonical_floor_rollback")
        if observed.registry_count < floor.registry_count:
            raise PolicyError("canonical_floor_rollback")
        # The ledger is pinned exactly: a read never adopts a consent row.
        if observed.ledger_digest != floor.ledger_digest or observed.ledger_sequence != floor.ledger_sequence:
            raise PolicyError("identity_ledger_unpinned")
        if observed.event_sequence == floor.event_sequence and observed.event_chain != floor.event_chain:
            raise PolicyError("canonical_floor_rollback")
        if observed.registry_count == floor.registry_count and observed.registry_digest != floor.registry_digest:
            raise PolicyError("canonical_floor_rollback")

    def _prefix_holds(self, conn, floor: CanonicalFloor) -> None:
        """The log the floor saw must still be the beginning of the log there is."""
        sequence, chain = event_chain(conn, through=floor.event_sequence)
        if sequence != floor.event_sequence or chain != floor.event_chain:
            raise PolicyError("canonical_floor_rollback")

    # --- the operations the node performs ------------------------------------

    def install(self, conn) -> CanonicalFloor:
        """First publication, for a node that has no floor yet."""
        if self.path.exists() or self.path.is_symlink():
            raise PolicyError("canonical_floor_unavailable")
        body = observe(conn, owner_id=self.owner_id, node_id=self.node_id, resource_id=self.resource_id,
                       revision=1)
        self._write(body)
        self._current, self._resume = body, (body.event_sequence, body.event_chain)
        return body

    def check(self, conn) -> CanonicalFloor:
        """Verify on a read, adopting only growth the node's own writers cause.

        The event log and the registry may have grown since the floor was
        written, because native writes advance them and do not publish. Those
        are adopted and republished. The ledger may not have moved at all.
        """
        floor = self._load()
        if floor.state != "active":
            raise PolicyError("canonical_floor_unavailable")
        self._prefix_holds(conn, floor)
        resume = self._resume if self._resume and self._resume[0] <= floor.event_sequence else None
        observed = observe(conn, owner_id=self.owner_id, node_id=self.node_id, resource_id=self.resource_id,
                           revision=floor.revision, resume=resume)
        self._compare(floor, observed)
        self._resume = (observed.event_sequence, observed.event_chain)
        if observed.model_dump(exclude={"revision"}) != floor.model_dump(exclude={"revision"}):
            advanced = observed.model_copy(update={"revision": floor.revision + 1})
            self._write(advanced)
            self._current = advanced
            return advanced
        self._current = floor
        return floor

    def publish_pending(self, conn) -> CanonicalFloor:
        """Called before a consent commit: the floor closes until it is completed."""
        floor = self.check(conn)
        pending = floor.model_copy(update={"state": "pending", "revision": floor.revision + 1})
        self._write(pending)
        self._current = pending
        return pending

    def publish_active(self, conn) -> CanonicalFloor:
        """Called after a consent commit, adopting the new ledger exactly once."""
        pending = self._load()
        if pending.state != "pending" or self._current is None or pending.revision != self._current.revision:
            raise PolicyError("canonical_floor_unavailable")
        self._prefix_holds(conn, pending)
        observed = observe(conn, owner_id=self.owner_id, node_id=self.node_id, resource_id=self.resource_id,
                           revision=pending.revision + 1)
        if (observed.clock_id != pending.clock_id or observed.generation < pending.generation
            or observed.event_sequence < pending.event_sequence
            or observed.ledger_sequence < pending.ledger_sequence
            or observed.registry_count < pending.registry_count):
            raise PolicyError("canonical_floor_rollback")
        self._write(observed)
        self._current, self._resume = observed, (observed.event_sequence, observed.event_chain)
        return observed


    def abort_pending(self, conn) -> CanonicalFloor:
        """Reopen a floor left pending by a consent write that did not happen.

        Only when the ledger is demonstrably where the pending floor left it. If
        a consent row did land, the two cannot be told apart from here and the
        floor stays closed for a deliberate recovery rather than guessing.
        """
        pending = self._load()
        if pending.state != "pending":
            raise PolicyError("canonical_floor_unavailable")
        self._prefix_holds(conn, pending)
        observed = observe(conn, owner_id=self.owner_id, node_id=self.node_id, resource_id=self.resource_id,
                           revision=pending.revision + 1)
        if (observed.clock_id != pending.clock_id or observed.ledger_digest != pending.ledger_digest
            or observed.ledger_sequence != pending.ledger_sequence
            or observed.generation < pending.generation or observed.event_sequence < pending.event_sequence
            or observed.registry_count < pending.registry_count):
            raise PolicyError("identity_ledger_unpinned")
        self._write(observed)
        self._current, self._resume = observed, (observed.event_sequence, observed.event_chain)
        return observed
