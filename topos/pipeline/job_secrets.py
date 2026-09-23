"""Secrets a queued job needs but its row must never hold.

A ``pipeline_jobs`` row outlives the request that created it — a finished row is
never deleted — and its ``payload_json`` is plain JSON. Two credentials used to
ride in it: ``progress_api_key``, which authenticates progress posts to the
control plane and defaulted to the node's shared engine key, and
``sync_options.signal_hex_key``, a Signal SQLCipher key a caller may supply when
the node cannot read Signal's own. Both sat at rest in every row that carried
them, readable by anything that could read the table.

``job_store.enqueue_job`` is the only writer of a job payload, so it withholds
them here: the row keeps the payload minus the secrets plus the NAMES of what was
withheld, and the values stay in this process's memory, keyed by job id, until
the job finishes. ``job_runner.process_job`` merges them back just before the
executor runs.

What a restart costs, by secret:

- ``progress_api_key`` — nothing. The control plane sends the key the engine
  connected with, which is the node's own ``settings.topos_key``; with no held
  value the runner uses that, exactly as ``start_ingestion`` always defaulted.
- ``sync_options.signal_hex_key`` — the key. The sync still tries Signal's own
  config and the keychain; when those fail, the error names the lost key so the
  person knows to supply it again (``withheld_secrets`` is what makes that
  message possible).

Keeping a caller-supplied key across restarts would mean storing it somewhere
durable, which is the defect this module exists to remove.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, Optional, Tuple

#: Dotted payload paths that carry credential values. One level of nesting is
#: all any current payload needs.
SECRET_PAYLOAD_PATHS: Tuple[str, ...] = ("progress_api_key", "sync_options.signal_hex_key")

#: Persisted beside the payload: the paths withheld, never their values.
WITHHELD_FIELD = "withheld_secrets"

_lock = threading.Lock()
_held: Dict[str, Dict[str, str]] = {}


def withhold(payload: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Split ``payload`` into what may be stored and the secrets it carried.

    Never mutates the caller's dict: both enqueue doors keep using theirs.
    An empty or non-string value is not a secret and is simply dropped.
    """
    stored: Dict[str, Any] = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    secrets: Dict[str, str] = {}
    withheld = set(stored.get(WITHHELD_FIELD) or [])
    for path in SECRET_PAYLOAD_PATHS:
        head, _, leaf = path.partition(".")
        parent = stored.get(head) if leaf else stored
        key = leaf or head
        if not isinstance(parent, dict) or key not in parent:
            continue
        value = parent.pop(key)
        if isinstance(value, str) and value.strip():
            secrets[path] = value
            withheld.add(path)
    if withheld:
        stored[WITHHELD_FIELD] = sorted(withheld)
    return stored, secrets


def hold(job_id: str, secrets: Dict[str, str]) -> None:
    """Keep ``secrets`` for ``job_id`` in memory. A later hold for the same job
    (a re-sent request) replaces the values it names and keeps the rest."""
    if not secrets:
        return
    with _lock:
        _held.setdefault(str(job_id), {}).update(secrets)


def restore(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The payload an executor sees: stored fields plus this job's held secrets.

    A job queued before this process started has none held. Its progress key
    then falls back to the node's own key — what ``start_ingestion`` always
    defaulted to — and only when the job reports progress at all.
    """
    out = dict(payload)
    with _lock:
        secrets = dict(_held.get(str(job_id)) or {})
    for path, value in secrets.items():
        head, _, leaf = path.partition(".")
        if leaf:
            nested = out.get(head)
            nested = dict(nested) if isinstance(nested, dict) else {}
            nested[leaf] = value
            out[head] = nested
        else:
            out[head] = value
    if out.get("progress_api_url") and not out.get("progress_api_key"):
        from ..config.settings import settings

        node_key = str(getattr(settings, "topos_key", "") or "").strip()
        if node_key:
            out["progress_api_key"] = node_key
    return out


def release(job_id: str) -> None:
    """Forget a job's secrets. Called once the job can no longer run again."""
    with _lock:
        _held.pop(str(job_id), None)


def peek(job_id: str) -> Dict[str, str]:
    """A copy of what is held for ``job_id`` (tests and diagnostics)."""
    with _lock:
        return dict(_held.get(str(job_id)) or {})


def clear() -> None:
    """Forget everything — what a process restart does."""
    with _lock:
        _held.clear()
