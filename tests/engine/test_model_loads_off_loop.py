"""The NER and embedding loads: from the local cache when it holds the model, on the loader
thread, never on an event loop's thread; the first batch awaits the load.

Measured on the owner's node (28 Oct 2026, raw import batches): the NER load went to the
Hub with every file on disk, and it ran inside the batch's worker, holding the cache's
loading mark for 20 s while the owner's frames queued on a loop that was busy beside it.
These tests drive fake loaders and fake libraries so no weights are needed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import threading
import time
import types

import pytest

from topos.engine import model_cache as MC
from topos.engine.backends import huggingface as HF

#: An owner frame's whole latency while a load runs beside it. The task's bound.
ALLOWED_DELAY = 0.1


@pytest.fixture(autouse=True)
def _fresh_cache():
    MC.reset_model_cache_for_tests()
    yield
    MC.reset_model_cache_for_tests()


@pytest.fixture
def cpu(monkeypatch):
    monkeypatch.setattr("topos.engine.torch_runtime.device_for", lambda component, preferred=None: "cpu")


def _slow_ner_loader(loaded_on, seconds):
    def loader(model, device):
        def _load():
            loaded_on.append(threading.current_thread().name)
            time.sleep(seconds)
            return ("ner", model, device)
        return _load
    return loader


async def _owner_frame() -> float:
    """A frame that needs the loop a few times; its whole latency is what the owner waits."""
    started = time.monotonic()
    for _ in range(5):
        await asyncio.sleep(0.001)
    return time.monotonic() - started


@pytest.mark.asyncio
async def test_a_slow_load_runs_on_the_loader_thread_and_a_concurrent_owner_frame_is_not_delayed(monkeypatch, cpu):
    loaded_on: list[str] = []
    monkeypatch.setattr(HF, "ner_loader", _slow_ner_loader(loaded_on, 1.0))
    load = asyncio.ensure_future(HF.warm_async("entity_extraction_batch", "example/ner"))
    await asyncio.sleep(0.05)                       # the load is under way
    delay = await _owner_frame()
    handle = await load
    assert delay < ALLOWED_DELAY, f"an owner frame waited {delay:.3f}s behind the load"
    assert len(loaded_on) == 1 and loaded_on[0].startswith("hf-model-loader")
    assert threading.current_thread().name not in loaded_on
    assert handle == ("ner", "example/ner", "cpu")
    # The first batch, run as the jobs run it, finds the model resident: no second load.
    again = await asyncio.to_thread(HF.HuggingFaceAdapter()._get_ner_pipeline, "example/ner")
    assert again is handle and len(loaded_on) == 1


@pytest.mark.asyncio
async def test_the_same_measurement_fails_for_a_load_left_on_the_loop_and_the_log_names_it(monkeypatch, cpu, caplog):
    loaded_on: list[str] = []
    monkeypatch.setattr(HF, "ner_loader", _slow_ner_loader(loaded_on, 0.5))
    frame = asyncio.ensure_future(_owner_frame())
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING, logger="topos.engine.huggingface"):
        HF.HuggingFaceAdapter()._get_ner_pipeline("example/ner-on-loop")   # synchronous, on the loop
    delay = await frame
    assert delay >= 0.4 and loaded_on == [threading.current_thread().name]
    assert any("requested on an event loop thread" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_two_callers_of_one_model_share_one_load(monkeypatch, cpu):
    loaded_on: list[str] = []
    monkeypatch.setattr(HF, "ner_loader", _slow_ner_loader(loaded_on, 0.3))
    first = asyncio.ensure_future(HF.warm_async("entity_extraction", "example/ner"))
    await asyncio.sleep(0.05)
    second = asyncio.to_thread(HF.HuggingFaceAdapter()._get_ner_pipeline, "example/ner")
    a, b = await asyncio.gather(first, second)
    assert a is b and len(loaded_on) == 1


def test_a_subtype_with_no_model_to_warm_is_refused():
    with pytest.raises(ValueError, match="no warmable model"):
        HF.warm("sentiment_classification")


# --------------------------------------------------------------------------- the first batch

class _Task:
    pass


@pytest.mark.asyncio
async def test_the_first_batch_awaits_the_loader_thread_and_runs_against_a_resident_model(monkeypatch, cpu):
    from topos.enrichment.jobs.canonical._engine_runner import run_engine_task

    loaded_on: list[str] = []
    monkeypatch.setattr(HF, "ner_loader", _slow_ner_loader(loaded_on, 0.3))
    ran: list[tuple[str, bool]] = []

    class FakeEngine:
        def inference_config(self, task):
            return {"subtype": task.subtype, "model": "example/ner"}

        def run(self, task):
            _, hit = MC.get_model_cache().acquire(MC.ModelSlot.NER, "example/ner@cpu", lambda: "a second load")
            ran.append((threading.current_thread().name, hit))
            return types.SimpleNamespace(status="completed", output={"items": []})

    frame = asyncio.ensure_future(_owner_frame())
    result = await run_engine_task(FakeEngine(), task_id="t1", subtype="entity_extraction_batch",
                                   source_id=None, record_ids=[], input_payload={"items": []})
    delay = await frame
    assert result.status == "completed"
    assert len(loaded_on) == 1 and loaded_on[0].startswith("hf-model-loader")
    assert ran == [(ran[0][0], True)] and ran[0][0] != threading.current_thread().name
    assert delay < ALLOWED_DELAY


@pytest.mark.asyncio
async def test_an_engine_that_cannot_name_its_model_is_left_alone(monkeypatch, cpu):
    from topos.enrichment.jobs.canonical._engine_runner import run_engine_task

    loads: list[str] = []
    monkeypatch.setattr(HF, "warm", lambda subtype, model=None: loads.append(subtype))

    class Bare:
        def run(self, task):
            return types.SimpleNamespace(status="completed", output={})

    await run_engine_task(Bare(), task_id="t2", subtype="entity_extraction_batch",
                         source_id=None, record_ids=[], input_payload={})
    assert loads == []


def test_the_raw_stage_starts_the_loads_its_jobs_will_need_and_no_others(monkeypatch):
    from topos.enrichment.jobs.canonical._engine_runner import warm_models_for_jobs

    started: list[tuple[str, str]] = []

    def fake_warm(subtype, model=None):
        started.append((subtype, model))
        return concurrent.futures.Future()

    monkeypatch.setattr(HF, "warm", fake_warm)

    class FakeEngine:
        def inference_config(self, task):
            return {"subtype": task.subtype, "model": f"model-for-{task.subtype}"}

    assert warm_models_for_jobs(["entities", "topics", "embeddings", "sentiment"], engine=FakeEngine()) == [
        "entity_extraction_batch", "embedding"]
    assert started == [("entity_extraction_batch", "model-for-entity_extraction_batch"),
                       ("embedding", "model-for-embedding")]


@pytest.mark.asyncio
async def test_the_raw_ingestion_stage_warms_before_the_pipeline_runs(monkeypatch):
    from topos.ingestion import ingest_helpers
    from topos.sources.registry import REGISTRY

    source_id = "imessage"
    if REGISTRY.get(source_id) is None:
        pytest.skip("no bundled source to run the stage against")
    order: list[str] = []
    monkeypatch.setattr("topos.enrichment.source_overrides.effective_canonical_enrichment_jobs",
                        lambda source, conn=None: ["entities", "embeddings"])
    monkeypatch.setattr("topos.enrichment.jobs.canonical._engine_runner.warm_models_for_jobs",
                        lambda jobs, engine=None: order.append(f"warm:{','.join(jobs)}") or [])

    async def pipeline(**kwargs):
        order.append("pipeline")
        return {"canonical_enrichment": {"jobs_run": 0}, "signal_derivation": {"jobs_run": 0}}

    monkeypatch.setattr(ingest_helpers, "run_post_canonical_pipeline", pipeline)
    out = await ingest_helpers._run_ui_payload_enrichment_inner(
        {"source_id": source_id, "canonical_records": [{"id": "an-invented-record"}]})
    assert out["status"] == "ok"
    assert order == ["warm:entities,embeddings", "pipeline"]


# --------------------------------------------------------------------------- offline first

CACHE_MISS = OSError("We couldn't connect to 'https://huggingface.co' to load the files, "
                     "and couldn't find them in the cached files.")


def _fake_transformers(monkeypatch, calls, *, cached=True):
    mod = types.ModuleType("transformers")

    def auto(name):
        class Auto:
            @classmethod
            def from_pretrained(cls, model_id, **kwargs):
                calls.append(("from_pretrained", name, model_id, kwargs))
                if not cached and kwargs.get("local_files_only"):
                    raise CACHE_MISS
                return (name, model_id)
        Auto.__name__ = name
        return Auto

    mod.AutoTokenizer = auto("AutoTokenizer")
    mod.AutoModelForTokenClassification = auto("AutoModelForTokenClassification")

    def pipeline(task, model=None, tokenizer=None, **kwargs):
        calls.append(("pipeline", task, model, tokenizer, kwargs))
        return ("pipeline", task, model)

    mod.pipeline = pipeline
    monkeypatch.setitem(sys.modules, "transformers", mod)


def test_the_ner_pipeline_loads_from_the_cache_with_no_hub_request(monkeypatch):
    calls: list = []
    _fake_transformers(monkeypatch, calls, cached=True)
    handle = HF.ner_loader("example/ner", "cpu")()
    offline = [c for c in calls if c[0] == "from_pretrained"]
    assert {c[1] for c in offline} == {"AutoTokenizer", "AutoModelForTokenClassification"}
    assert all(c[3] == {"local_files_only": True} for c in offline)
    built = [c for c in calls if c[0] == "pipeline"]
    assert built == [("pipeline", "ner", ("AutoModelForTokenClassification", "example/ner"),
                      ("AutoTokenizer", "example/ner"), {"aggregation_strategy": "first", "device": "cpu"})]
    assert handle == ("pipeline", "ner", ("AutoModelForTokenClassification", "example/ner"))


def test_a_cold_cache_still_loads_from_the_hub_with_the_same_settings(monkeypatch):
    calls: list = []
    _fake_transformers(monkeypatch, calls, cached=False)
    HF.ner_loader("example/ner", "cpu")()
    built = [c for c in calls if c[0] == "pipeline"]
    assert built == [("pipeline", "ner", "example/ner", None, {"aggregation_strategy": "first", "device": "cpu"})]


def _fake_sentence_transformers(monkeypatch, calls):
    mod = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))

    mod.SentenceTransformer = SentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)


def test_the_embedding_model_loads_from_its_cached_snapshot(monkeypatch):
    calls: list = []
    _fake_sentence_transformers(monkeypatch, calls)
    monkeypatch.setattr(HF, "cached_model_path", lambda model: "/a/cache/snapshot" if model == "example/embed" else None)
    HF.embedding_loader("example/embed", "cpu", {})()
    HF.embedding_loader("example/embed-cold", "cpu", {"trust_remote_code": True})()
    assert calls == [("/a/cache/snapshot", {"device": "cpu", "local_files_only": True}),
                     ("example/embed-cold", {"device": "cpu", "trust_remote_code": True})]


def test_the_cache_is_asked_without_a_request_and_a_miss_answers_none(monkeypatch):
    asked: list = []
    mod = types.ModuleType("huggingface_hub")

    def snapshot_download(repo_id, **kwargs):
        asked.append((repo_id, kwargs))
        if repo_id == "example/missing":
            raise FileNotFoundError("not in the cache")
        return "/a/cache/" + repo_id

    mod.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", mod)
    assert HF.cached_model_path("example/present") == "/a/cache/example/present"
    assert HF.cached_model_path("example/missing") is None
    assert all(kwargs == {"local_files_only": True} for _, kwargs in asked)
