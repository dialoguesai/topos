"""HuggingFace backend adapter: url_classification and emotion_classification."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.engine.huggingface")

# Default models (same as current website_classifier and emo_27_job)
DEFAULT_URL_CLASSIFICATION_MODEL = "KnutJaegersberg/website-classifier"
DEFAULT_EMOTION_MODEL = "SamLowe/roberta-base-go_emotions"


class HuggingFaceAdapter:
    """BackendAdapter for HuggingFace: text-classification pipeline and go_emotions model."""

    def __init__(self) -> None:
        self._url_pipeline: Any = None
        self._url_lock = threading.Lock()
        self._emotion_model: Any = None
        self._emotion_tokenizer: Any = None
        self._emotion_loaded = False
        self._emotion_lock = threading.Lock()

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
        if subtype in ("emotion_classification", "emo_27"):
            return self._run_emotion_classification(payload, model)
        # Unknown subtype: return error-like output so formatter can set status
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
