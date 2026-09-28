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
            self._thread = threading.Thread(target=lambda:context.run(self._run, request),
                                            name="permissions-auto-review", daemon=True)
            self._thread.start()
            return self._status.model_copy(deep=True)

    def _update(self, **counts):
        with self._lock:
            self._status = self._status.model_copy(update={key:getattr(self._status,key)+value
                                                         for key,value in counts.items()})

    def _page(self, table, after_id, request):
        # Only constant table names reach this helper. Time bounds limit existing
        # data reads; this is not a request to sync any history from the source.
        if table not in {"conversation_messages", "ai_chat_messages"}:
            raise PolicyError("unsupported_message_table")
        with self.resolver._read() as (conn, _):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not {"message_id","source_id","event_at","content","conversation_id"} <= columns:
                raise PolicyError("message_schema_unavailable")
            dataset = "dataset_id" if table == "conversation_messages" else "NULL"
            return [tuple(row) for row in conn.execute(
                f"SELECT message_id,source_id,{dataset} FROM {table} WHERE message_id>? "
                "AND julianday(event_at)>=julianday(?,'unixepoch') "
                "AND julianday(event_at)<=julianday(?,'unixepoch') ORDER BY message_id LIMIT 200",
                (after_id,request.after,request.before)).fetchall()]

    def _run(self, request):
        state = "complete"
        try:
            asyncio.run(self._process(request))
        except Exception:
            # No raw exception or model output: it may contain private content.
            state = "failed"
        finally:
            if self._stop.is_set():
                state = "cancelled"
            with self._lock:
                self._status = self._status.model_copy(update={"state":state})

    async def _process(self, request):
        consecutive_unavailable = 0
        for table in ("conversation_messages", "ai_chat_messages"):
            after_id = ""
            while not self._stop.is_set():
                try:
                    page = self._page(table, after_id, request)
                except PolicyError:
                    self._update(unresolved=1)
                    break
                if not page:
                    break
                for record_id, source_id, dataset_id in page:
                    if self._stop.is_set():
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
                        self._update(assessed=1)
                    except PolicyError:
                        self._update(unresolved=1)
                    except Exception:
                        self._update(unresolved=1)
                        consecutive_unavailable += 1
                        if consecutive_unavailable >= 3:
                            raise PolicyError("machine_classifier_unavailable") from None
                # Optional local index refresh is outside any read/write gate.
                if self.refresh and not self._stop.is_set():
                    self.refresh()
