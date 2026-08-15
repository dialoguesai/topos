"""Every Lab-runnable job's engine subtype must resolve a Lab override.

The Enrichment Lab's "apply preferred model" stores an override keyed by JOB id;
``run_engine_task`` looks it up by engine SUBTYPE through ``SUBTYPE_TO_JOB``.
Those are two vocabularies, and nothing bound them together: ``entities_job``
emits ``entity_extraction_batch`` while the map carried only the bare
``entity_extraction``, so an entities override was stored, displayed in the Lab,
and silently never applied. emo_27 had already hit the identical hole (its
``_batch`` rename) and was patched by hand; entities was missed.

These tests derive the required map entries from the code that actually runs —
the Lab's own factory dict and each job module's ``run_engine_task`` calls — so
the next subtype rename fails here instead of orphaning another override.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import tempfile
from pathlib import Path

import pytest

import topos.enrichment.jobs as jobs_pkg
import topos.enrichment_lab.worker as lab_worker
from topos.enrichment.model_overrides import (
    SUBTYPE_TO_JOB,
    get_override_for_subtype,
    set_model_override,
)


def _lab_job_classes() -> dict[str, str]:
    """job_id -> Job class name, read from lab worker's own factories dict."""
    tree = ast.parse(inspect.getsource(lab_worker))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "factories" in targets and isinstance(node.value, ast.Dict):
                return {
                    k.value: v.id
                    for k, v in zip(node.value.keys, node.value.values)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Name)
                }
    raise AssertionError("factories dict not found in enrichment_lab.worker")


def _engine_subtypes(cls_name: str) -> set[str]:
    """Subtype literals passed to run_engine_task in the job class's module."""
    module = inspect.getmodule(getattr(jobs_pkg, cls_name))
    tree = ast.parse(Path(module.__file__).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "run_engine_task":
            continue
        for kw in node.keywords:
            if kw.arg == "subtype" and isinstance(kw.value, ast.Constant):
                found.add(str(kw.value.value))
    return found


def test_every_lab_job_subtype_resolves_its_override() -> None:
    lab_jobs = _lab_job_classes()
    assert lab_jobs, "Lab offers no jobs? The extraction is broken."
    missing: list[str] = []
    for job_id, cls_name in lab_jobs.items():
        for subtype in _engine_subtypes(cls_name):
            if SUBTYPE_TO_JOB.get(subtype) != job_id:
                missing.append(f"{job_id}: subtype {subtype!r} not mapped to it")
    assert not missing, (
        "Lab overrides for these jobs are stored but can never be applied — "
        "run_engine_task looks up by subtype and finds nothing:\n  "
        + "\n  ".join(missing)
    )


@pytest.fixture
def conn():
    db = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    yield db
    db.close()


def test_entities_override_reaches_its_batch_subtype(conn) -> None:
    """The concrete regression: store for the job, resolve by the real subtype."""
    set_model_override("entities", "huggingface", "someone/preferred-ner", conn=conn)
    got = get_override_for_subtype("entity_extraction_batch", conn=conn)
    assert got is not None, "override stored for entities but invisible to its subtype"
    assert got["model"] == "someone/preferred-ner"
