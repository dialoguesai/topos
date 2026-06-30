"""HuggingFace backend adapter: url/emotion/entity/embedding/sentiment classification."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.engine.huggingface")

# Default models (same as current website_classifier and emo_27_job)
DEFAULT_URL_CLASSIFICATION_MODEL = "KnutJaegersberg/website-classifier"
DEFAULT_EMOTION_MODEL = "SamLowe/roberta-base-go_emotions"
DEFAULT_NER_MODEL = "dslim/bert-base-NER"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_SENTIMENT_MODEL = "cardiffnlp/twitter-roberta-base-sentiment-latest"


class HuggingFaceAdapter:
    """BackendAdapter for HuggingFace: text-classification pipeline and go_emotions model."""

    def __init__(self) -> None:
        self._url_pipeline: Any = None
        self._url_lock = threading.Lock()
        self._emotion_model: Any = None
        self._emotion_tokenizer: Any = None
        self._emotion_loaded = False
        self._emotion_lock = threading.Lock()
        self._ner_pipeline: Any = None
        self._ner_lock = threading.Lock()
        self._embedding_model: Any = None
        self._embedding_lock = threading.Lock()
        self._sentiment_pipeline: Any = None
        self._sentiment_lock = threading.Lock()

    def load_model(self, model_name: str, config: Optional[Dict[str, Any]] = None) -> None:
        """Load model by name; we load on first run_inference per subtype instead."""
        pass

    def ensure_model(self, model_name: str, subtype: Optional[str] = None) -> bool:
        """
        Ensure the model is downloaded (e.g. from HuggingFace Hub). Downloads if not present.
        Returns True if a download was triggered (caller may clean up cache later), False if already in cache.
        Logs when download starts; Hub may show progress via tqdm if enabled.
        """
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            return False
        logger.info("Downloading model %s (huggingface)...", model_name)
        try:
            # tqdm_enabled=True lets HuggingFace show a progress bar when available
            snapshot_download(repo_id=model_name, tqdm_enabled=True)
        except Exception:
            logger.exception("Failed to download model %s", model_name)
            return False
        logger.info("Model %s (huggingface) download complete.", model_name)
        return True

    def _get_url_pipeline(self, model_name: str):
        with self._url_lock:
            if self._url_pipeline is not None:
                return self._url_pipeline
            from transformers import pipeline
            self._url_pipeline = pipeline(
                task="text-classification",
                model=model_name,
            )
            return self._url_pipeline

    def _get_emotion_model(self, model_name: str):
        with self._emotion_lock:
            if self._emotion_loaded:
                return self._emotion_model, self._emotion_tokenizer
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            import torch
            self._emotion_tokenizer = AutoTokenizer.from_pretrained(model_name)
            self._emotion_model = AutoModelForSequenceClassification.from_pretrained(model_name)
            self._emotion_model.eval()
            self._emotion_loaded = True
            return self._emotion_model, self._emotion_tokenizer

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch by config.subtype to url_classification or emotion_classification."""
        config = config or {}
        subtype = config.get("subtype") or ""
        model = config.get("model") or ""

        if subtype == "url_classification":
            return self._run_url_classification(payload, model)
        if subtype == "url_classification_batch":
            return self._run_url_classification_batch(payload, model)
        if subtype in ("emotion_classification", "emo_27"):
            return self._run_emotion_classification(payload, model)
        if subtype == "entity_extraction":
            return self._run_entity_extraction(payload, model)
        if subtype == "embedding":
            return self._run_embedding(payload, model, config)
        if subtype == "sentiment_classification":
            return self._run_sentiment_classification(payload, model)
        if subtype == "privacy_disclosure":
            return self._run_privacy_disclosure(payload, model)
        if subtype == "content_nsfw_classification":
            return self._run_content_nsfw_classification(payload, model)
        return {"error": f"Unknown subtype: {subtype}", "status": "unsupported"}

    def _run_url_classification(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        """Same behavior as WebsiteUrlClassifier."""
        url = payload.get("url") or ""
        title = payload.get("title") or ""
        if not isinstance(url, str) or not url.strip():
            return {"error": "url must be a non-empty string", "category": "unknown", "confidence": 0.0, "model": model_name or DEFAULT_URL_CLASSIFICATION_MODEL}
        model = model_name or DEFAULT_URL_CLASSIFICATION_MODEL
        pipeline = self._get_url_pipeline(model)
        clean_url = url.strip()
        clean_title = (title or "").strip()
        text = f"{clean_url} [SEP] {clean_title}" if clean_title else clean_url
        result = pipeline(text, truncation=True, top_k=1)
        top_result = result[0] if isinstance(result, list) and result else {}
        return {
            "category": top_result.get("label", "unknown"),
            "confidence": float(top_result.get("score", 0.0) or 0.0),
            "model": model,
        }

    def _run_url_classification_batch(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        items = payload.get("items") or []
        if not isinstance(items, list) or not items:
            return {"error": "items required", "items": [], "model": model_name or DEFAULT_URL_CLASSIFICATION_MODEL}
        model = model_name or DEFAULT_URL_CLASSIFICATION_MODEL
        pipeline = self._get_url_pipeline(model)
        texts: List[str] = []
        for item in items:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url:
                texts.append("")
                continue
            texts.append(f"{url} [SEP] {title}" if title else url)

        non_empty_idx = [i for i, t in enumerate(texts) if t]
        classified: List[Any] = []
        if non_empty_idx:
            batch_texts = [texts[i] for i in non_empty_idx]
            classified = pipeline(batch_texts, truncation=True, top_k=1)
            if not isinstance(classified, list):
                classified = [classified]

        out_items: List[Dict[str, Any]] = []
        cls_ptr = 0
        for text in texts:
            if not text:
                out_items.append({"category": "unknown", "confidence": 0.0, "model": model})
                continue
            top_result = classified[cls_ptr] if cls_ptr < len(classified) else {}
            cls_ptr += 1
            if isinstance(top_result, list) and top_result:
                top_result = top_result[0]
            out_items.append(
                {
                    "category": top_result.get("label", "unknown"),
                    "confidence": float(top_result.get("score", 0.0) or 0.0),
                    "model": model,
                }
            )
        return {"items": out_items, "model": model}

    def _run_emotion_classification(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        """Same behavior as Emo27Job._classify_emotion."""
        text = payload.get("text") or payload.get("content") or ""
        if not text or not isinstance(text, str):
            return {"error": "text or content required", "emotion_label": None, "confidence": None, "all_emotions": [], "model": model_name or DEFAULT_EMOTION_MODEL}
        model = model_name or DEFAULT_EMOTION_MODEL
        import torch
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

    def _get_ner_pipeline(self, model_name: str):
        with self._ner_lock:
            if self._ner_pipeline is not None:
                return self._ner_pipeline
            from transformers import pipeline

            model = model_name or DEFAULT_NER_MODEL
            self._ner_pipeline = pipeline("ner", model=model, aggregation_strategy="simple")
            return self._ner_pipeline

    def _get_embedding_model(self, model_name: str):
        with self._embedding_lock:
            if self._embedding_model is not None:
                return self._embedding_model
            from sentence_transformers import SentenceTransformer

            model = model_name or DEFAULT_EMBEDDING_MODEL
            self._embedding_model = SentenceTransformer(model)
            return self._embedding_model

    def _get_sentiment_pipeline(self, model_name: str):
        with self._sentiment_lock:
            if self._sentiment_pipeline is not None:
                return self._sentiment_pipeline
            from transformers import pipeline

            model = model_name or DEFAULT_SENTIMENT_MODEL
            self._sentiment_pipeline = pipeline("sentiment-analysis", model=model)
            return self._sentiment_pipeline

    def _run_entity_extraction(self, payload: Dict[str, Any], model_name: str) -> Dict[str, Any]:
        text = payload.get("text") or payload.get("content") or ""
        if not text or not isinstance(text, str):
            return {"entities": [], "model": model_name or DEFAULT_NER_MODEL, "provider": "huggingface"}
        model = model_name or DEFAULT_NER_MODEL
        ner = self._get_ner_pipeline(model)
        raw = ner(text)
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
                "model": model_name or DEFAULT_EMBEDDING_MODEL,
                "provider": "huggingface",
                "normalized": embedding_normalize_enabled(),
            }
        model = model_name or DEFAULT_EMBEDDING_MODEL
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
        """Clear cached pipeline/model (simplified: clear if name matches)."""
        if model_name == DEFAULT_URL_CLASSIFICATION_MODEL or "website-classifier" in (model_name or ""):
            with self._url_lock:
                self._url_pipeline = None
        if model_name == DEFAULT_EMOTION_MODEL or "go_emotions" in (model_name or ""):
            with self._emotion_lock:
                self._emotion_model = None
                self._emotion_tokenizer = None
                self._emotion_loaded = False
