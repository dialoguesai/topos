"""Concurrent load against one scratch node: owner queries, analytics reads, a health probe, then recovery.

Every request goes to the node's own doors, as the owner (the scratch socket) or
over loopback TCP for the health probe; there is no control plane. The mix is
aimed at the class the post-install stress run kept finding: work that leaves
the event loop for a pool thread while the loop keeps using the database.

* ``query`` in ``summary`` mode through ``/api/local/agent/dispatch``: the
  session purge, retrieval, session ``put`` and artifact INSERT each hop to a
  worker (``topos/query/pipeline.py``) while the loop reads the session and the
  installed sources. No model is called in this mode.
* ``complexity_*``, ``graph_cypher_query`` and ``tools_index_status``: CPU-bound
  reads that the handlers move to workers.
* ``GET /healthcheck`` every half second: an event-loop stall shows as a slow or
  failed probe.

One record per request, with its outcome and SQLite's error text when the node
returned one; never any response body.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import random
import time
from typing import Optional

from .seed import FEEL, TOPICS, VERBS, WHEN

#: (scope, question template). Templates use only the seed's invented vocabulary.
QUESTIONS = [
    ("messages:read", "What did I talk about this week about the {topic}?"),
    ("messages:read", "Who did I message about the {topic} last month?"),
    ("work_context:read", "What am I working on lately? Anything about the {topic}?"),
    ("schedule:read", "What is on my schedule {when}?"),
    ("activity:read", "What have I been reading about the {topic}?"),
    ("transcripts:read", "What was said in my recordings about the {topic}?"),
    ("health:read", "How have I been feeling {when}? I felt {feel}."),
    ("ai_conversations:read", "What did I ask the assistant about the {topic}?"),
    ("relationship_context:read", "Who do I talk to most about the {topic}?"),
    ("facts:read", "What do I know about the {topic}?"),
    ("graph:read", "Who is connected to the {topic}?"),
    ("contacts:resolve", "Find the contact I {verb} the {topic} with."),
]
ANALYTICS = [
    ("complexity_summary", {"weeks": 12}),
    ("complexity_topics_daily", {"days": 90, "top": 10}),
    ("graph_cypher_query", {"query": "MATCH (p:person) RETURN p LIMIT 25"}),
    ("tools_index_status", {}),
]
#: SQLite's own words for one connection used by two threads, or for a lock held too long.
SQLITE_SIGNATURES = (
    "another row available", "no more rows available", "recursive use of cursors",
    "cannot start a transaction within a transaction", "cannot commit - no transaction is active",
    "cannot rollback - no transaction is active",
    "sql statements in progress", "bad parameter or other api misuse", "library routine called out of sequence",
    "not an error", "database is locked", "database table is locked", "sql logic error", "interfaceerror",
)


def sqlite_signature(text: str) -> Optional[str]:
    low = (text or "").lower()
    return next((s for s in SQLITE_SIGNATURES if s in low), None)


@dataclass
class Record:
    kind: str
    name: str
    phase: str
    at_s: float
    elapsed_s: float
    ok: bool
    http_status: Optional[int] = None
    outcome: Optional[str] = None
    error: Optional[str] = None
    sqlite_error: Optional[str] = None
    transport_error: Optional[str] = None


@dataclass
class Profile:
    """Defaults: three minutes of load. See docs/PRE_TAG_LOAD_GATE.md for why these numbers."""
    load_s: float = 180.0
    query_workers: int = 12
    analytics_workers: int = 2
    analytics_pause_s: float = 1.0
    health_interval_s: float = 0.5
    request_timeout_s: float = 180.0
    drain_s: float = 120.0
    recovery_idle_s: float = 10.0
    recovery_probes: int = 5
    #: No healthy probe for this long means the node is hung: stop the load, skip recovery, let the gate dump stacks.
    hung_after_s: float = 60.0
    seed: int = 7
    extra: dict = field(default_factory=dict)


class Driver:
    def __init__(self, *, socket_path: str, port: int, profile: Profile):
        self.socket_path = socket_path
        self.port = port
        self.profile = profile
        self.records: list[Record] = []
        self.t0 = time.monotonic()
        self.last_health_ok = 0.0
        self.hung_at: Optional[float] = None

    def _now(self) -> float:
        return time.monotonic() - self.t0

    async def _dispatch(self, client, kind: str, name: str, payload: dict, phase: str) -> Record:
        """One dispatcher request. ``kind`` "query" sends type ``query`` (``name`` is its scope); any other kind
        sends ``name`` as the type."""
        started = self._now()
        rec = Record(kind=kind, name=name, phase=phase, at_s=round(started, 3), elapsed_s=0.0, ok=False)
        msg_type = "query" if kind == "query" else name
        try:
            r = await client.post("/api/local/agent/dispatch", json={"type": msg_type, "payload": payload},
                                  timeout=self.profile.request_timeout_s)
            rec.http_status = r.status_code
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and body.get("status") == "ok":
                inner = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                rec.outcome = str(inner.get("turn_outcome") or "ok")
                rec.ok = rec.outcome not in ("error", "failed")
            else:
                error = str(body.get("error") or body.get("detail") or f"http_{r.status_code}")
                rec.error = error[:200]
            rec.sqlite_error = sqlite_signature(rec.error or "")
        except asyncio.CancelledError:
            # Still in flight when the drain window closed or the node hung: a failure, not nothing.
            rec.transport_error = "cancelled_unfinished"
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is a record, never a crash
            rec.transport_error = type(exc).__name__
        finally:
            rec.elapsed_s = round(self._now() - started, 3)
            self.records.append(rec)
        return rec

    def _question(self, rng: random.Random) -> tuple[str, dict]:
        scope, template = rng.choice(QUESTIONS)
        text = template.format(topic=rng.choice(TOPICS), when=rng.choice(WHEN), feel=rng.choice(FEEL),
                               verb=rng.choice(VERBS))
        return scope, {"scope_id": scope, "query": text, "access_mode": "summary"}

    async def _query_worker(self, client, index: int, until: float) -> None:
        rng = random.Random(self.profile.seed * 1000 + index)
        while self._now() < until:
            scope, payload = self._question(rng)
            await self._dispatch(client, "query", scope, payload, "load")

    async def _analytics_worker(self, client, index: int, until: float) -> None:
        i = index
        while self._now() < until:
            name, payload = ANALYTICS[i % len(ANALYTICS)]
            i += 1
            await self._dispatch(client, "analytics", name, payload, "load")
            await asyncio.sleep(self.profile.analytics_pause_s)

    async def _health(self, client, phase: str) -> Record:
        started = self._now()
        rec = Record(kind="health", name="healthcheck", phase=phase, at_s=round(started, 3), elapsed_s=0.0, ok=False)
        try:
            r = await client.get("/healthcheck", timeout=15)
            rec.http_status = r.status_code
            db_ok = r.json().get("db_ok") if r.status_code == 200 else False
            # The node's own reading: None is "the probe's SELECT 1 did not get a pool thread in time", which it
            # reports as unknown, not unhealthy (core/db_health.py). Counted, not failed; False fails.
            rec.ok = r.status_code == 200 and db_ok is not False
            if rec.ok:
                self.last_health_ok = self._now()
                rec.outcome = "db_ok" if db_ok else "db_unknown"
            else:
                rec.error = f"http_{r.status_code}"
        except Exception as exc:  # noqa: BLE001
            rec.transport_error = type(exc).__name__
        rec.elapsed_s = round(self._now() - started, 3)
        self.records.append(rec)
        return rec

    async def _health_worker(self, client, until: float) -> None:
        while self._now() < until:
            await self._health(client, "load")
            await asyncio.sleep(self.profile.health_interval_s)

    async def _watch_for_hang(self, workers: list) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self._now() - self.last_health_ok > self.profile.hung_after_s:
                self.hung_at = round(self._now(), 1)
                for task in workers:
                    task.cancel()
                return

    async def run(self) -> dict:
        import httpx

        p = self.profile
        limits = httpx.Limits(max_connections=p.query_workers + p.analytics_workers + 4)
        uds = httpx.AsyncHTTPTransport(uds=self.socket_path, limits=limits)
        async with httpx.AsyncClient(transport=uds, base_url="http://localhost") as owner, \
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}") as tcp:
            self.t0 = time.monotonic()
            until = p.load_s
            workers = [asyncio.create_task(self._query_worker(owner, i, until)) for i in range(p.query_workers)]
            workers += [asyncio.create_task(self._analytics_worker(owner, i, until))
                        for i in range(p.analytics_workers)]
            health = asyncio.create_task(self._health_worker(tcp, until + p.drain_s))
            watch = asyncio.create_task(self._watch_for_hang(workers + [health]))
            # Workers stop starting requests at `until`; in-flight ones get the drain window to finish.
            done, pending = await asyncio.wait(workers, timeout=p.load_s + p.drain_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            load_end = self._now()
            health.cancel()
            watch.cancel()
            await asyncio.gather(health, watch, return_exceptions=True)
            if self.hung_at is None:
                # Recovery: idle, then sequential probes that must each succeed. A hung node gets none: the gate
                # dumps its stacks instead of waiting out a dozen timeouts.
                await asyncio.sleep(p.recovery_idle_s)
                rng = random.Random(p.seed)
                for _ in range(p.recovery_probes):
                    _, payload = self._question(rng)
                    await self._dispatch(owner, "query", payload["scope_id"], payload, "recovery")
                    await self._health(tcp, "recovery")
        return {"load_end_s": round(load_end, 1), "stragglers_cancelled": len(pending), "hung_at_s": self.hung_at,
                "records": [asdict(r) for r in self.records]}


def run(*, socket_path: str, port: int, profile: Profile) -> dict:
    return asyncio.run(Driver(socket_path=socket_path, port=port, profile=profile).run())
