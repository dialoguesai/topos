"""ChatGPT source definitions include signal_derivation_jobs with embeddings."""

from topos.sources.registry import CHATGPT_FILE, CHATGPT_UI


def test_chatgpt_sources_include_embeddings_signal_job() -> None:
    assert "embeddings" in CHATGPT_FILE.signal_derivation_jobs
    assert "embeddings" in CHATGPT_UI.signal_derivation_jobs
