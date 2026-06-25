"""ChatGPT source definitions include resolved signal jobs with embeddings."""

from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs
from topos.sources.registry import CHATGPT_FILE, CHATGPT_UI


def test_chatgpt_sources_include_embeddings_signal_job() -> None:
    assert "embeddings" in resolved_signal_derivation_jobs(CHATGPT_FILE)
    assert "embeddings" in resolved_signal_derivation_jobs(CHATGPT_UI)
