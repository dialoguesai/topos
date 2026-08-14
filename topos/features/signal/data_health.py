"""Per-dimension data health: continuous volume/freshness/brief/model blend.

The score is a graded 0-1 signal-richness reading, not a coverage cliff:

    score = 0.45*volume + 0.25*freshness + 0.15*brief + 0.15*model_readiness

- volume:    1 - 0.5^(n/target) over real signal rows + stamped embeddings
             (saturating curve; per-dimension targets in dimension_registry).
- freshness: 24h half-life decay from the newest signal row / embedding.
- brief:     7d half-life decay from the last real brief revision (empty
             initial shells at revision 1 earn nothing).
- model:     fraction of the dimension's enrichment jobs whose model is
             actually usable — Ollama reachable and above the size minimum,
             HuggingFace weights present in the local cache. Per-job detail
             rides along in `model_readiness_jobs`.

Honesty rules: stub rows (model *_stub_v1, provider "stub") and deferred
placeholders never count; dimensions with zero real signal report
measured=False and get no health row.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...enrichment.job_readiness import jobs_for_provider, readiness_of
from ...storage.adapters.factory import AdapterBundle
from .dimension_registry import (
    DEFAULT_VOLUME_TARGET,
    DIMENSION_SIGNAL_OBJECTS,
    DIMENSION_VOLUME_TARGETS,
    SIGNAL_DIMENSIONS,
)

logger = logging.getLogger("topos.features.signal.data_health")

WEIGHT_VOLUME = 0.45
WEIGHT_FRESHNESS = 0.25
WEIGHT_BRIEF = 0.15
WEIGHT_MODEL = 0.15

BRIEF_HALF_LIFE_HOURS = 7 * 24.0


def _llm_jobs() -> frozenset:
    """Signal jobs bound to the LLM provider, read from the model catalog.

    Was a literal frozenset here, which is a second copy of something the
    catalog already states — and the kind that goes stale silently when a job
    is added or repointed to another provider.
    """
    return jobs_for_provider("ollama")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def freshness_score(latest_ts: Optional[str], *, half_life_hours: float = 24.0) -> float:
    """Decay from max signal created_at vs now (24h half-life default)."""
    ts = _parse_ts(latest_ts)
    if ts is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    return float(math.pow(0.5, age_hours / half_life_hours))


def volume_score(count: int, target: int) -> float:
    """Saturating volume curve: 50% at target rows, asymptotic to 1.0."""
    if count <= 0:
        return 0.0
    return float(1.0 - math.pow(0.5, count / max(1, target)))


def is_stub_signal_row(item: Dict[str, Any]) -> bool:
    """True for placeholder rows that must not count as measured signal."""
    if item.get("_deferred"):
        return True
    model = str(item.get("model") or "").strip().lower()
    provider = str(item.get("provider") or "").strip().lower()
    return model.endswith("_stub_v1") or provider == "stub"


def check_provider_status() -> Dict[str, str]:
    """Reachability of provider SERVICES (not inferred from deferred jobs).

    ``huggingface`` is always "up" and that is not a claim about models: there
    is no HuggingFace service to reach, the library runs in-process. Whether a
    given job's weights are actually on disk is a per-model question, answered
    by ``enrichment.job_readiness`` — do not read this key as "the HF jobs can
    run".
    """
    try:
        from ...engine.backends.ollama import OllamaAdapter

        ollama = "up" if OllamaAdapter().is_reachable() else "down"
    except Exception:
        ollama = "down"
    return {"ollama": ollama, "huggingface": "up"}


def _jobs_for_dimension(dim_id: str) -> List[str]:
    """Dimension-critical enrichment jobs (signal objects that are job ids)."""
    try:
        from ...enrichment.catalog import get_enrichment_catalog

        catalog = get_enrichment_catalog()
    except Exception:
        return []
    jobs = [obj for obj in DIMENSION_SIGNAL_OBJECTS.get(dim_id, []) if obj in catalog]
    # Every dimension's living brief depends on the LLM summary job.
    if "dimension_summary" not in jobs:
        jobs.append("dimension_summary")
    return jobs


def _model_readiness(
    jobs: List[str],
    *,
    llm_remote: bool,
    llm_meets_minimum: bool,
    provider_status: Optional[Dict[str, str]] = None,
) -> Tuple[float, List[Dict[str, Any]]]:
    """Per-job model availability for a dimension, and the fraction ready.

    This used to be a bare fraction that counted every non-LLM job ready
    unconditionally — "HuggingFace task models and rules jobs run in-process on
    any supported device", which is true only once the weights are on disk. A
    machine set up offline has no weights cached and was still reported at
    100% model readiness for every dimension those jobs feed.

    Returning the per-job detail is the other half: it makes an empty dimension
    explainable ("the NER weights are not downloaded yet") rather than merely
    low-scoring, which is the question a person actually has.
    """
    if not jobs:
        return 1.0, []
    details: List[Dict[str, Any]] = []
    for job in jobs:
        state = readiness_of(job, provider_status=provider_status)
        ready, reason = state.ready, state.reason
        if state.provider == "ollama":
            # Routing the engine does that the catalog does not describe: a
            # configured remote provider serves these whatever is installed
            # locally, and a local model below the minimum does not.
            if llm_remote:
                ready, reason = True, "remote LLM provider"
            elif ready and not llm_meets_minimum:
                ready, reason = False, "installed model below minimum"
        entry = state.as_dict()
        entry["ready"] = ready
        entry["reason"] = reason
        details.append(entry)
    return sum(1 for d in details if d["ready"]) / len(details), details


class DataHealthComputer:
    """Continuous per-dimension health over all 10 signal dimensions."""

    def __init__(
        self,
        adapters: AdapterBundle,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._adapters = adapters
        self._conn = conn

    def _embedding_stats(self, dim_id: str) -> tuple[int, Optional[str]]:
        """(count, latest created_at) of stamped embeddings for a dimension."""
        if self._conn is not None:
            try:
                row = self._conn.execute(
                    "SELECT COUNT(*), MAX(created_at) FROM signal_embeddings"
                    " WHERE signal_dimension=?",
                    (dim_id,),
                ).fetchone()
                if row:
                    return int(row[0] or 0), row[1]
            except sqlite3.Error:
                pass
        try:
            page = self._adapters.vector.list_metadata(limit=1, offset=0, dimension=dim_id)
            return int(page.total or 0), None
        except Exception:
            return 0, None

    def _signal_stats(self, dim_id: str) -> tuple[int, Optional[str]]:
        """(count, latest created_at) of real signal rows, stubs excluded."""
        try:
            page = self._adapters.signal.get_by_dimension(dim_id, limit=1000, offset=0)
        except Exception:
            return 0, None
        count = 0
        latest: Optional[str] = None
        for item in page.items:
            if is_stub_signal_row(item):
                continue
            count += 1
            ts = item.get("created_at")
            if ts and (latest is None or ts > latest):
                latest = ts
        if self._conn is not None and count > 0:
            # SQLite payloads rarely carry created_at; the column does.
            try:
                for table in ("signal_facts", "signal_scores"):
                    row = self._conn.execute(
                        f"SELECT MAX(created_at) FROM {table} WHERE dimension=?",  # noqa: S608
                        (dim_id,),
                    ).fetchone()
                    if row and row[0] and (latest is None or row[0] > latest):
                        latest = row[0]
            except sqlite3.Error:
                pass
        return count, latest

    def _brief_score(self, dim_id: str) -> tuple[float, Optional[int], Optional[str]]:
        """(decayed score, revision, updated_at) — revision 1 is an empty shell."""
        if self._conn is None:
            return 0.0, None, None
        try:
            row = self._conn.execute(
                "SELECT revision_number, updated_at FROM signal_dimension_briefs"
                " WHERE signal_dimension=?",
                (dim_id,),
            ).fetchone()
        except sqlite3.Error:
            return 0.0, None, None
        if not row:
            return 0.0, None, None
        revision = int(row[0] or 0)
        if revision < 2:
            return 0.0, revision, row[1]
        return (
            freshness_score(row[1], half_life_hours=BRIEF_HALF_LIFE_HOURS),
            revision,
            row[1],
        )

    def compute(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        deferred_jobs = deferred_jobs or []
        provider_status = check_provider_status()
        ollama_up = provider_status.get("ollama") == "up"

        provider_failures: List[str] = []
        # Deferred LLM jobs in this run are direct evidence of an unreachable
        # provider, corroborating (or preempting) the live probe.
        if not ollama_up or any(j in deferred_jobs for j in _llm_jobs()):
            provider_failures.append("ollama_unreachable")

        try:
            from .model_recommendations import signal_model_recommendation

            model_rec = signal_model_recommendation(self._conn)
        except Exception as exc:  # noqa: BLE001
            logger.debug("model recommendation unavailable: %s", exc)
            model_rec = {"provider": "ollama", "meets_minimum": True}
        llm_remote = str(model_rec.get("provider") or "ollama") != "ollama"
        llm_meets_minimum = bool(model_rec.get("meets_minimum", True))

        profiles: Dict[str, Dict[str, Any]] = {}
        for dim in SIGNAL_DIMENSIONS:
            dim_id = dim["id"]
            signal_count, latest = self._signal_stats(dim_id)
            emb_count, emb_latest = self._embedding_stats(dim_id)
            signal_count += emb_count
            if emb_latest and (latest is None or emb_latest > latest):
                latest = emb_latest

            target = DIMENSION_VOLUME_TARGETS.get(dim_id, DEFAULT_VOLUME_TARGET)
            volume = volume_score(signal_count, target)
            freshness = freshness_score(latest)
            brief, brief_revision, brief_updated_at = self._brief_score(dim_id)
            jobs = _jobs_for_dimension(dim_id)
            model_readiness, model_jobs = _model_readiness(
                jobs,
                llm_remote=llm_remote,
                llm_meets_minimum=llm_meets_minimum,
                # The probe this method already took, so ten dimensions do not
                # re-ask ten times and cannot disagree with `provider_failures`.
                provider_status=provider_status,
            )
            measured = signal_count > 0
            # Unmeasured dimensions score 0: model readiness alone must not
            # paint a bar for a dimension that has no data.
            score = (
                WEIGHT_VOLUME * volume
                + WEIGHT_FRESHNESS * freshness
                + WEIGHT_BRIEF * brief
                + WEIGHT_MODEL * model_readiness
            ) if measured else 0.0
            profiles[dim_id] = {
                "id": dim_id,
                "label": dim["label"],
                "score": round(score, 4),
                # coverage_score keeps its historical meaning (volume) for
                # existing consumers (grant scope panel, dimensions API).
                "coverage_score": round(volume, 4),
                "volume_score": round(volume, 4),
                "freshness_score": round(freshness, 4),
                "brief_score": round(brief, 4),
                "model_readiness_score": round(model_readiness, 4),
                # Which job is waiting on what. The score alone cannot say
                # whether a dimension is dark because a server is missing or
                # because a download has not happened yet.
                "model_readiness_jobs": model_jobs,
                "signal_count": signal_count,
                "volume_target": target,
                "brief_revision": brief_revision,
                "brief_updated_at": brief_updated_at,
                "required_jobs": jobs,
                "measured": measured,
                "canonical_sources": DIMENSION_SIGNAL_OBJECTS.get(dim_id, []),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "provider_failures": list(provider_failures),
            }
        return profiles
