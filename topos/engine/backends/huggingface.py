"""HuggingFace backend adapter: url/emotion/entity/embedding/sentiment classification."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ..model_cache import ModelSlot, get_model_cache

logger = logging.getLogger("topos.engine.huggingface")

# Default models (same as emo_27_job)
DEFAULT_EMOTION_MODEL = "SamLowe/roberta-base-go_emotions"
DEFAULT_NER_MODEL = "djagatiya/ner-roberta-base-ontonotesv5-englishv4"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_SENTIMENT_MODEL = "cardiffnlp/twitter-roberta-base-sentiment-latest"
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Retrieval-tuned embedding models are asymmetric: queries and passages get
# different prefixes. Symmetric models (MiniLM) use empty prefixes.
EMBEDDING_MODEL_PROFILES: Dict[str, Dict[str, Any]] = {
    "sentence-transformers/all-MiniLM-L6-v2": {
        "dims": 384,
        "query_prefix": "",
        "passage_prefix": "",
    },
    "BAAI/bge-small-en-v1.5": {
        "dims": 384,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage_prefix": "",
    },
    # Snowflake Arctic Embed (Apache-2.0): retrieval-tuned, query-side prefix only.
    # arctic-s is 384d — a drop-in for the MiniLM vec table width (re-embed, no schema
    # change). arctic-m-v1.5 is 768d (Performance tier — needs a new vec0 table). See
    # PLAN_NODE_UPGRADE_AND_EVAL_EXPANSION.md §E1/§E1b.
    "Snowflake/snowflake-arctic-embed-s": {
        "dims": 384,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage_prefix": "",
    },
    "Snowflake/snowflake-arctic-embed-m-v1.5": {
        "dims": 768,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage_prefix": "",
    },
    "BAAI/bge-base-en-v1.5": {
        "dims": 768,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage_prefix": "",
    },
    "intfloat/e5-small-v2": {
        "dims": 384,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
    "nomic-ai/nomic-embed-text-v1.5": {
        "dims": 768,
        "query_prefix": "search_query: ",
        "passage_prefix": "search_document: ",
        "trust_remote_code": True,
    },
}


def active_embedding_model() -> str:
    """Embedding model for new writes and query embedding (env-switchable)."""
    return os.environ.get("TOPOS_EMBED_MODEL", "").strip() or DEFAULT_EMBEDDING_MODEL


def embedding_model_profile(model_name: Optional[str] = None) -> Dict[str, Any]:
    name = (model_name or active_embedding_model()).strip()
    return EMBEDDING_MODEL_PROFILES.get(name, {"query_prefix": "", "passage_prefix": ""})


def apply_embedding_prefix(texts: List[str], *, model_name: Optional[str], input_role: str) -> List[str]:
    profile = embedding_model_profile(model_name)
    prefix = str(profile.get(f"{input_role}_prefix") or "") if input_role in ("query", "passage") else ""
    if not prefix:
        return texts
    return [f"{prefix}{t}" for t in texts]


_MODEL_NAME_TO_SLOT: Dict[str, ModelSlot] = {
    DEFAULT_EMOTION_MODEL: ModelSlot.EMOTION,
    DEFAULT_NER_MODEL: ModelSlot.NER,
    DEFAULT_EMBEDDING_MODEL: ModelSlot.EMBEDDING,
    DEFAULT_SENTIMENT_MODEL: ModelSlot.SENTIMENT,
    DEFAULT_RERANK_MODEL: ModelSlot.RERANKER,
}


def _match_slot(model_name: str) -> Optional[ModelSlot]:
    name = (model_name or "").strip().lower()
    if not name:
        return None
    for key, slot in _MODEL_NAME_TO_SLOT.items():
        if key.lower() == name or key.split("/")[-1].lower() in name:
            return slot
    if "go_emotions" in name or "emotion" in name:
        return ModelSlot.EMOTION
    if "ner" in name:
        return ModelSlot.NER
    if "minilm" in name or "embedding" in name or "sentence-transformers" in name:
        return ModelSlot.EMBEDDING
    if "sentiment" in name:
        return ModelSlot.SENTIMENT
    return None


# Sentence/bracket punctuation trimmed off span edges after word snapping.
_EDGE_TRIM_CHARS = ".,;:!?…\"'()[]{}"


def _stitch_wordpiece_entities(
    text: str, raw: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Repair wordpiece-split NER output against the ORIGINAL text.

    Even with word-level aggregation, defensive: (1) merge contiguous
    "##fragment" detections into their anchor, (2) snap every span to word
    boundaries in the source text and rebuild the surface from it (fixes both
    "##ny" junk AND the sneakier "Jon"-for-"Jonny" silent truncation),
    (3) dedupe overlapping detections of the same word keeping the best score.
    Entities without char offsets pass through untouched.
    """
    if not text or not raw:
        return raw
    if any(not isinstance(e.get("start"), int) or not isinstance(e.get("end"), int) for e in raw):
        return raw

    ordered = sorted(raw, key=lambda e: (int(e["start"]), int(e["end"])))

    # 1) merge contiguous ## fragments into the previous entity
    merged: List[Dict[str, Any]] = []
    for ent in ordered:
        word = str(ent.get("word") or "")
        prev = merged[-1] if merged else None
        if (
            word.startswith("##")
            and prev is not None
            and int(ent["start"]) <= int(prev["end"])
        ):
            prev["end"] = max(int(prev["end"]), int(ent["end"]))
            prev["score"] = max(float(prev.get("score") or 0.0), float(ent.get("score") or 0.0))
            continue  # anchor keeps its entity_group
        merged.append(dict(ent))

    # 2) snap spans to word boundaries; rebuild surfaces from the source text
    n = len(text)
    for ent in merged:
        start, end = int(ent["start"]), int(ent["end"])
        start = max(0, min(start, n))
        end = max(start, min(end, n))
        while start > 0 and text[start - 1].isalnum():
            start -= 1
        while end < n and text[end].isalnum():
            end += 1
        # Byte-level tokenizers (roberta) merge adjacent punctuation into the
        # word token, so sentence-final spans arrive as "Austin." — trim edge
        # punctuation AFTER snapping. Conservative set: sentence/bracket marks
        # only, so identifier-ish surfaces (@handle, C++) keep their symbols.
        while end > start and text[end - 1] in _EDGE_TRIM_CHARS:
            end -= 1
        while start < end and text[start] in _EDGE_TRIM_CHARS:
            start += 1
        ent["start"], ent["end"] = start, end
        ent["word"] = text[start:end]

    # 3) dedupe identical spans (overlapping detections snap together); drop empties
    best: Dict[tuple, Dict[str, Any]] = {}
    for ent in merged:
        surface = str(ent.get("word") or "").strip()
        if not surface or surface.startswith("##"):
            continue
        key = (ent["start"], ent["end"])
        incumbent = best.get(key)
        if incumbent is None or float(ent.get("score") or 0.0) > float(incumbent.get("score") or 0.0):
            best[key] = ent
    return [best[k] for k in sorted(best)]


class HuggingFaceAdapter:
    """BackendAdapter for HuggingFace: text-classification pipeline and go_emotions model."""

    def load_model(self, model_name: str, config: Optional[Dict[str, Any]] = None) -> None:
        """Load model by name; we load on first run_inference per subtype instead."""
        pass

    def ensure_model(self, model_name: str, subtype: Optional[str] = None) -> bool:
        """
        Ensure the model is downloaded (e.g. from HuggingFace Hub). Downloads if not present.
        Returns True if a download was triggered (caller may clean up cache later), False if already in cache.
        """
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            return False
        logger.info("Downloading model %s (huggingface)...", model_name)
        try:
            snapshot_download(repo_id=model_name, tqdm_enabled=False)
        except Exception:
            logger.exception("Failed to download model %s", model_name)
            return False
        logger.info("Model %s (huggingface) download complete.", model_name)
        return True


    def _get_emotion_model(self, model_name: str) -> Tuple[Any, Any]:
        model = model_name or DEFAULT_EMOTION_MODEL

        def _load():
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(model)
            emo_model = AutoModelForSequenceClassification.from_pretrained(model)
            emo_model.eval()
            return emo_model, tokenizer

        handle, _ = get_model_cache().acquire(ModelSlot.EMOTION, model, _load)
        return handle

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch by config.subtype to the matching task pipeline."""
        config = config or {}
        subtype = config.get("subtype") or ""
        model = config.get("model") or ""

        if subtype in ("emotion_classification", "emo_27"):
            return self._run_emotion_classification(payload, model)
        if subtype == "emotion_classification_batch":
            return self._run_emotion_classification_batch(payload, model)
        if subtype == "entity_extraction":
            return self._run_entity_extraction(payload, model)
        if subtype == "entity_extraction_batch":
            return self._run_entity_extraction_batch(payload, model)
        if subtype == "embedding":
            return self._run_embedding(payload, model, config)
        if subtype == "rerank":
            return self._run_rerank(payload, model)
        if subtype == "sentiment_classification":
            return self._run_sentiment_classification(payload, model)
        if subtype == "privacy_disclosure":
            return self._run_privacy_disclosure(payload, model)
        if subtype == "content_nsfw_classification":
            return self._run_content_nsfw_classification(payload, model)
        return {"error": f"Unknown subtype: {subtype}", "status": "unsupported"}



    def _classify_emotion_text(self, text: str, model_name: str) -> Dict[str, Any]:
        import torch

        model = model_name or DEFAULT_EMOTION_MODEL
        emo_model, tokenizer = self._get_emotion_model(model)
        inputs = tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=True,
        )
        with torch.no_grad():
            outputs = emo_model(**inputs)
            probabilities = torch.nn.functional.softmax(outputs.logits[0], dim=-1)
        labels = getattr(emo_model.config, "id2label", {}) or {}
        top_k = min(5, len(labels))
        top_probs, top_indices = torch.topk(probabilities, top_k)
        all_emotions = []
        for prob, idx in zip(top_probs, top_indices):
            label_id = idx.item()
            label = labels.get(label_id, f"label_{label_id}")
            confidence = prob.item()
            if confidence > 0.1:
                all_emotions.append({"label": label, "confidence": float(confidence)})
        top = all_emotions[0] if all_emotions else None
        return {
            "emotion_label": top["label"] if top else None,
            "confidence": top["confidence"] if top else None,
            "all_emotions": all_emotions,
            "model": model,
        }

    def _run_emotion_classification(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        text = payload.get("text") or payload.get("content") or ""
        if not text or not isinstance(text, str):
            return {
                "error": "text or content required",
                "emotion_label": None,
                "confidence": None,
                "all_emotions": [],
                "model": model_name or DEFAULT_EMOTION_MODEL,
            }
        return self._classify_emotion_text(text, model_name)

    def _run_emotion_classification_batch(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        items = payload.get("items") or []
        if not isinstance(items, list) or not items:
            return {"error": "items required", "items": [], "model": model_name or DEFAULT_EMOTION_MODEL}
        model = model_name or DEFAULT_EMOTION_MODEL
        out_items: List[Dict[str, Any]] = []
        for item in items:
            text = str(item.get("text") or item.get("content") or "")
            item_id = item.get("id") or item.get("message_id")
            if not text:
                out_items.append(
                    {
                        "id": item_id,
                        "emotion_label": None,
                        "confidence": None,
                        "all_emotions": [],
                        "model": model,
                    }
                )
                continue
            result = self._classify_emotion_text(text, model)
            out_items.append({"id": item_id, **result})
        return {"items": out_items, "model": model}

    def _get_ner_pipeline(self, model_name: str):
        model = model_name or DEFAULT_NER_MODEL

        def _load():
            from transformers import pipeline

            # "first": WORD-level aggregation — every subword takes the tag of
            # its word's first piece, so entities align to whole words and
            # "##fragment" outputs become structurally impossible. ("simple"
            # only merged adjacent same-tag TOKENS: any mid-word tag flip split
            # the word — 5.2% of live raw hits were ## junk, and the kept
            # halves were silently truncated surfaces like "Jon" for "Jonny".)
            return pipeline("ner", model=model, aggregation_strategy="first")

        handle, _ = get_model_cache().acquire(ModelSlot.NER, model, _load)
        return handle

    def _get_embedding_model(self, model_name: str):
        model = model_name or active_embedding_model()
        profile = EMBEDDING_MODEL_PROFILES.get(model, {})

        def _load():
            from sentence_transformers import SentenceTransformer

            kwargs: Dict[str, Any] = {}
            if profile.get("trust_remote_code"):
                kwargs["trust_remote_code"] = True
            return SentenceTransformer(model, **kwargs)

        handle, _ = get_model_cache().acquire(ModelSlot.EMBEDDING, model, _load)
        return handle

    def _get_rerank_model(self, model_name: str):
        model = model_name or DEFAULT_RERANK_MODEL

        def _load():
            from sentence_transformers import CrossEncoder

            return CrossEncoder(model)

        handle, _ = get_model_cache().acquire(ModelSlot.RERANKER, model, _load)
        return handle

    def _get_sentiment_pipeline(self, model_name: str):
        model = model_name or DEFAULT_SENTIMENT_MODEL

        def _load():
            from transformers import pipeline

            return pipeline("sentiment-analysis", model=model)

        handle, _ = get_model_cache().acquire(ModelSlot.SENTIMENT, model, _load)
        return handle

    def _run_entity_extraction(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        text = payload.get("text") or payload.get("content") or ""
        if not text or not isinstance(text, str):
            return {"entities": [], "model": model_name or DEFAULT_NER_MODEL, "provider": "huggingface"}
        model = model_name or DEFAULT_NER_MODEL
        ner = self._get_ner_pipeline(model)
        raw = _stitch_wordpiece_entities(text, list(ner(text) or []))
        entities: List[Dict[str, Any]] = []
        for ent in raw or []:
            entities.append(
                {
                    "entity_text": ent.get("word") or ent.get("entity"),
                    "entity_type": ent.get("entity_group") or ent.get("entity"),
                    "confidence": float(ent.get("score", 0.0) or 0.0),
                    "start": ent.get("start"),
                    "end": ent.get("end"),
                }
            )
        return {"entities": entities, "model": model, "provider": "huggingface"}

    def _run_entity_extraction_batch(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        """Batched NER: payload.items = [{id, text}]; one pipeline call per batch."""
        items = payload.get("items") or []
        if not isinstance(items, list) or not items:
            return {"error": "items required", "items": [], "model": model_name or DEFAULT_NER_MODEL}
        model = model_name or DEFAULT_NER_MODEL
        ner = self._get_ner_pipeline(model)
        texts = [str(item.get("text") or item.get("content") or "") for item in items]
        non_empty_idx = [i for i, t in enumerate(texts) if t.strip()]
        raw_batches: List[Any] = []
        if non_empty_idx:
            raw_batches = ner([texts[i] for i in non_empty_idx])
            # A single-text call returns a flat entity list rather than a list of lists.
            if raw_batches and isinstance(raw_batches[0], dict):
                raw_batches = [raw_batches]
        out_items: List[Dict[str, Any]] = []
        batch_ptr = 0
        for idx, item in enumerate(items):
            item_id = item.get("id") or item.get("message_id")
            if idx not in non_empty_idx:
                out_items.append({"id": item_id, "entities": []})
                continue
            raw = raw_batches[batch_ptr] if batch_ptr < len(raw_batches) else []
            batch_ptr += 1
            raw = _stitch_wordpiece_entities(texts[idx], list(raw or []))
            entities: List[Dict[str, Any]] = []
            for ent in raw or []:
                entities.append(
                    {
                        "entity_text": ent.get("word") or ent.get("entity"),
                        "entity_type": ent.get("entity_group") or ent.get("entity"),
                        "confidence": float(ent.get("score", 0.0) or 0.0),
                        "start": ent.get("start"),
                        "end": ent.get("end"),
                    }
                )
            out_items.append({"id": item_id, "entities": entities})
        return {"items": out_items, "model": model, "provider": "huggingface"}

    def _run_embedding(
        self, payload: Dict[str, Any], model_name: str, config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        from ...features.signal.vector_settings import embedding_normalize_enabled

        texts = payload.get("texts")
        if texts is None:
            single = payload.get("text") or payload.get("content") or ""
            texts = [single] if single else []
        if not isinstance(texts, list):
            texts = [str(texts)]
        texts = [str(t) for t in texts if t]
        if not texts:
            return {
                "vectors": [],
                "dims": 0,
                "model": model_name or active_embedding_model(),
                "provider": "huggingface",
                "normalized": embedding_normalize_enabled(),
            }
        model = model_name or active_embedding_model()
        input_role = str(
            (config or {}).get("input_role") or payload.get("input_role") or ""
        ).strip().lower()
        if input_role in ("query", "passage"):
            texts = apply_embedding_prefix(texts, model_name=model, input_role=input_role)
        batch_size = int((config or {}).get("batch_size") or payload.get("batch_size") or 32)
        embedder = self._get_embedding_model(model)
        normalize = embedding_normalize_enabled()
        vectors: List[List[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            encoded = embedder.encode(
                batch,
                convert_to_numpy=True,
                normalize_embeddings=normalize,
                show_progress_bar=False,
            )
            for row in encoded:
                vectors.append([float(x) for x in row.tolist()])
        dims = len(vectors[0]) if vectors else 0
        return {
            "vectors": vectors,
            "dims": dims,
            "model": model,
            "provider": "huggingface",
            "normalized": normalize,
        }

    def _run_rerank(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        """Cross-encoder rerank: payload = {query, candidates: [{id, text}]}."""
        query = str(payload.get("query") or "").strip()
        candidates = payload.get("candidates") or []
        model = model_name or DEFAULT_RERANK_MODEL
        if not query or not isinstance(candidates, list) or not candidates:
            return {"error": "query and candidates required", "items": [], "model": model}
        pairs = [(query, str(c.get("text") or "")) for c in candidates]
        try:
            reranker = self._get_rerank_model(model)
            scores = reranker.predict(pairs, show_progress_bar=False)
        except Exception as exc:
            logger.debug("rerank unavailable (%s); returning input order", exc)
            return {
                "items": [
                    {"id": c.get("id"), "rerank_score": None} for c in candidates
                ],
                "model": model,
                "provider": "huggingface",
                "status": "unavailable",
            }
        items = [
            {"id": c.get("id"), "rerank_score": float(s)}
            for c, s in zip(candidates, scores)
        ]
        items.sort(key=lambda item: item["rerank_score"], reverse=True)
        return {"items": items, "model": model, "provider": "huggingface"}

    def _run_content_nsfw_classification(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        from ...sanitization.nsfw_classifier import classify_nsfw_batch

        items = payload.get("items") or []
        if not isinstance(items, list):
            return {"error": "items required", "status": "invalid", "items": []}
        result = classify_nsfw_batch(items)
        if model_name and result.get("status") == "ok":
            result["model"] = model_name
        return result

    def _run_privacy_disclosure(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        from ...sanitization.privacy_filter import redact_privacy_batch

        items = payload.get("items") or []
        if not isinstance(items, list):
            return {"error": "items required", "status": "invalid", "items": []}
        transform_id = str(payload.get("transform_id") or "pii_redaction")
        result = redact_privacy_batch(items, transform_id=transform_id)
        if model_name and result.get("status") == "ok":
            result["model"] = model_name
        return result

    def _run_sentiment_classification(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        text = payload.get("text") or payload.get("content") or ""
        if not text or not isinstance(text, str):
            return {
                "label": None,
                "score": None,
                "model": model_name or DEFAULT_SENTIMENT_MODEL,
                "provider": "huggingface",
            }
        model = model_name or DEFAULT_SENTIMENT_MODEL
        try:
            pipe = self._get_sentiment_pipeline(model)
            result = pipe(text[:512], truncation=True)
            top = result[0] if isinstance(result, list) and result else {}
            return {
                "label": top.get("label"),
                "score": float(top.get("score", 0.0) or 0.0),
                "model": model,
                "provider": "huggingface",
            }
        except Exception:
            return self._run_emotion_classification(payload, DEFAULT_EMOTION_MODEL)

    def unload_model(self, model_name: str) -> None:
        """Evict cached pipeline/model for the given model name."""
        slot = _match_slot(model_name)
        if slot is not None:
            get_model_cache().evict_slot(slot)

    def unload_all(self) -> None:
        for slot in ModelSlot:
            if slot not in (ModelSlot.NSFW, ModelSlot.PRIVACY_FILTER):
                get_model_cache().evict_slot(slot)
