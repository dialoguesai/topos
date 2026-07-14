"""MVP default model registrations for Wiki signal derivation (PRD §6.3)."""

from __future__ import annotations

from .registry import ModelRegistry

MVP_JOB_SPECS = [
    ("emo_27", "emotion_classification", "huggingface", "SamLowe/roberta-base-go_emotions", True),
    # OntoNotes-18 NER (was CoNLL-4 dslim/bert-base-NER): unlocks
    # work_of_art/event/product spine types; value labels dropped in
    # map_ner_type. Plain token-classification head — no CRF, so the vanilla
    # transformers pipeline decodes it correctly.
    ("entities", "entity_extraction", "huggingface", "djagatiya/ner-roberta-base-ontonotesv5-englishv4", True),
    ("embeddings", "embedding", "huggingface", "sentence-transformers/all-MiniLM-L6-v2", True),
    ("sentiment", "sentiment_classification", "huggingface", "cardiffnlp/twitter-roberta-base-sentiment-latest", True),
    ("url_classification", "url_classification", "huggingface", "facebook/bart-large-mnli", True),
    ("topics", "topic_extraction", "ollama", "llama3.2", True),
    ("dimension_summary", "raw_to_summary", "ollama", "llama3.2", True),
    ("goal_extraction", "goal_extraction", "ollama", "llama3.2", True),
    ("relationship_edges", "relationship_scoring", "rules", "", True),
    ("availability_scores", "availability_scoring", "rules", "", True),
]


def load_mvp_defaults(registry: ModelRegistry) -> None:
    """Register all MVP signal derivation jobs with provider defaults."""
    for job_id, task_name, provider, model_path, preferred in MVP_JOB_SPECS:
        common = {
            "model_id": f"mvp/{job_id}",
            "model_name": job_id,
            "model_version": "mvp",
            "task_name": task_name,
            "is_preferred": preferred,
        }
        if provider == "huggingface":
            registry.register_model(
                **common,
                model_type="signal_derivation",
                provider="huggingface",
                huggingface_path=model_path,
            )
        elif provider == "ollama":
            registry.register_model(
                **common,
                model_type="signal_derivation",
                provider="ollama",
                ollama_model=model_path,
            )
        else:
            registry.register_model(
                **common,
                model_type="rules_only",
                provider="huggingface",
                metadata={"rules_only": True},
            )
