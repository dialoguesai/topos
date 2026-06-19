"""MVP default model registrations cover PRD §6.3 job catalog."""

from topos.enrichment.models.mvp_defaults import MVP_JOB_SPECS, load_mvp_defaults
from topos.enrichment.models.registry import ModelRegistry


def test_model_registry_mvp_defaults_registers_all_jobs() -> None:
    registry = ModelRegistry()
    load_mvp_defaults(registry)
    registered = {spec[0] for spec in MVP_JOB_SPECS}
    for job_id in registered:
        model = registry.get_model(f"mvp/{job_id}")
        assert model is not None, job_id
    assert len(registry.list_models()) == len(MVP_JOB_SPECS)
