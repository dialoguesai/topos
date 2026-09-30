"""Bounded owner-started background assessment. Assessments are durable checkpoints.

On restart/re-run, scan the requested window and skip exact current assessments.
No source ingestion, no fixed first-page loop, and no owner review is overwritten.
This preparatory worker grants no access and cannot be started by a recipient.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time

from .automatic_message_review import prepare, assess, publish, is_current, machine_key
from .canonical import PolicyError
from .evidence import _owner
from .message_review_contract import AutomaticReviewStatus


class AutomaticReviewWorker:
    def __init__(self, resolver, reviews, *, classifier=assess, refresh=None):
        self.resolver, self.reviews = resolver, reviews
        self.classifier, self.refresh = classifier, refresh
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._status = AutomaticReviewStatus(state="idle")

    def status(self):
        _owner(self.resolver.binding)
        with self._lock:
            return self._status.model_copy(deep=True)

    def cancel(self):
        _owner(self.resolver.binding)
        self._stop.set()
        return self.status()

    def close(self):
        self._stop.set()

    def start(self, request, *, now=None):
        """The owner's pass over the requested window. Refreshes the indexes after each page."""
        return self._launch(request, now=now, refresh=True)

    def start_node_pass(self, request, *, now=None, ingested_after=None, max_assessed=None):
        """The node's own catch-up pass (refresh_loop.py). Same checks, same durable checkpoints.

        It never rebuilds an index: every new assessment moves the review digest, the sweep
        drops the index as drift, and the node's restore rebuilds it once passes are idle.
        `ingested_after` (UTC seconds) limits the pass to conversations that received a row
        after that time, so the neighbours whose context a new row changed are re-checked too.
        `max_assessed` bounds local-model calls in one pass.
        """
        if max_assessed is not None and (type(max_assessed) is not int or max_assessed < 1):
            raise PolicyError("message_review_budget_invalid")
        if ingested_after is not None and (type(ingested_after) is not int or ingested_after < 0):
            raise PolicyError("message_review_window_invalid")
        return self._launch(request, now=now, refresh=False, ingested_after=ingested_after,
                            max_assessed=max_assessed)

    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def _launch(self, request, *, now, refresh, ingested_after=None, max_assessed=None):
        _owner(self.resolver.binding)
        now = int(time.time()) if now is None else now
        if request.before > now or request.before <= request.after or request.before-request.after > 31*86400:
            raise PolicyError("message_review_window_invalid")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._status.model_copy(deep=True)
            self._stop.clear()
            self._status = AutomaticReviewStatus(state="running")
            context = contextvars.copy_context()
            options = dict(refresh=refresh, ingested_after=ingested_after, max_assessed=max_assessed)
            self._thread = threading.Thread(target=lambda:context.run(self._run, request, **options),
                                            name="permissions-auto-review", daemon=True)
            self._thread.start()
            return self._status.model_copy(deep=True)

    def _update(self, **counts):
        with self._lock:
            self._status = self._status.model_copy(update={key:getattr(self._status,key)+value
                                                         for key,value in counts.items()})

    def _page(self, table, after_id, request, ingested_after=None):
        # Only constant table names reach this helper. Time bounds limit existing
        # data reads; this is not a request to sync any history from the source.
        if table == "journal_entries":
            return self._journal_page(after_id, request, ingested_after)
        if table not in {"conversation_messages", "ai_chat_messages"}:
            raise PolicyError("unsupported_message_table")
        with self.resolver._read() as (conn, _):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            required = {"message_id","source_id","event_at","content","conversation_id"}
            if ingested_after is not None:
                required = required | {"ingested_at"}
            if not required <= columns:
                raise PolicyError("message_schema_unavailable")
            dataset = "dataset_id" if table == "conversation_messages" else "NULL"
            scope, args = "", [after_id, request.after, request.before]
            if ingested_after is not None:
                # Whole conversations: a new row changes its neighbours' context revision.
                scope = (f" AND conversation_id IN (SELECT conversation_id FROM {table} "
                         "WHERE julianday(ingested_at)>julianday(?,'unixepoch'))")
                args.append(ingested_after)
            return [tuple(row) for row in conn.execute(
                f"SELECT message_id,source_id,{dataset} FROM {table} WHERE message_id>? "
                "AND julianday(event_at)>=julianday(?,'unixepoch') "
                f"AND julianday(event_at)<=julianday(?,'unixepoch'){scope} ORDER BY message_id LIMIT 200",
                args).fetchall()]

    def _journal_page(self, after_id, request, ingested_after=None):
        """Journal entries to assess (IF-5 §1.1). Their time is naive text, read here as UTC with a day of
        slack either side: this only chooses what to assess, never what a grant may release."""
        with self.resolver._read() as (conn, _):
            columns = {r[1] for r in conn.execute("PRAGMA table_info(journal_entries)")}
            if not {"entry_id", "source_id", "entry_at", "content", "ingested_at"} <= columns:
                raise PolicyError("message_schema_unavailable")
            scope, args = "", [after_id, request.after - 86_400, request.before + 86_400]
            if ingested_after is not None:
                scope = " AND julianday(ingested_at)>julianday(?,'unixepoch')"
                args.append(ingested_after)
            return [tuple(row) for row in conn.execute(
                "SELECT entry_id,source_id,NULL FROM journal_entries WHERE entry_id>? "
                "AND julianday(entry_at)>=julianday(?,'unixepoch') "
                f"AND julianday(entry_at)<=julianday(?,'unixepoch'){scope} ORDER BY entry_id LIMIT 200",
                args).fetchall()]

    def _run(self, request, **options):
        state = "complete"
        try:
            asyncio.run(self._process(request, **options))
        except Exception:
            # No raw exception or model output: it may contain private content.
            state = "failed"
        finally:
            if self._stop.is_set():
                state = "cancelled"
            with self._lock:
                self._status = self._status.model_copy(update={"state":state})

    async def _process(self, request, *, refresh=True, ingested_after=None, max_assessed=None):
        consecutive_unavailable = 0
        assessed = 0
        from .evidence_families import enabled_tables
        for table in enabled_tables():
            after_id = ""
            while not self._stop.is_set():
                if max_assessed is not None and assessed >= max_assessed:
                    return
                try:
                    page = self._page(table, after_id, request, ingested_after)
                except PolicyError:
                    self._update(unresolved=1)
                    break
                if not page:
                    break
                for record_id, source_id, dataset_id in page:
                    if self._stop.is_set() or (max_assessed is not None and assessed >= max_assessed):
                        return
                    after_id = record_id
                    self._update(scanned=1)
                    identity = self.resolver._identity(table, record_id, source_id, dataset_id)
                    try:
                        prepared = prepare(self.resolver, self.reviews, identity)
                        with self.reviews._db() as db:
                            previous = self.reviews._current_in(db, machine_key(identity))
                        if is_current(previous, prepared):
                            self._update(current=1)
                            continue
                    except PolicyError:
                        self._update(withheld=1)
                        continue
                    try:
                        labels = await self.classifier(prepared)
                        if self._stop.is_set():
                            return
                        publish(self.resolver, self.reviews, prepared, labels, now=int(time.time()))
                        consecutive_unavailable = 0
                        assessed += 1
                        self._update(assessed=1)
                    except PolicyError:
                        self._update(unresolved=1)
                    except Exception:
                        self._update(unresolved=1)
                        consecutive_unavailable += 1
                        if consecutive_unavailable >= 3:
                            raise PolicyError("machine_classifier_unavailable") from None
                # Optional local index refresh is outside any read/write gate. Owner passes only.
                if refresh and self.refresh and not self._stop.is_set():
                    self.refresh()
