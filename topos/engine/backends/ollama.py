"""Ollama backend adapter: HTTP API for local LLM inference."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.engine.ollama")

_PROMPT_CONFIG: Optional[Dict[str, Any]] = None


def _load_prompt_config() -> Dict[str, Any]:
    global _PROMPT_CONFIG
    if _PROMPT_CONFIG is not None:
        return _PROMPT_CONFIG
    config_path = Path(__file__).resolve().parents[2] / "enrichment" / "signal_derivation_config.json"
    try:
        _PROMPT_CONFIG = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        _PROMPT_CONFIG = {}
    return _PROMPT_CONFIG


class OllamaAdapter:
    """BackendAdapter for Ollama (http://localhost:11434)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        if base_url is None:
            try:
                from ...config.settings import settings
                base_url = getattr(settings, "engine_ollama_base_url", None) or "http://localhost:11434"
            except Exception:
                base_url = "http://localhost:11434"
        self._base_url = str(base_url).rstrip("/")

    def list_models(self) -> List[str]:
        """Return list of model names available on the server (from /api/tags)."""
        req = urllib.request.Request(f"{self._base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception:
            return []

    def pull_model(self, model_name: str, *, stream: bool = True) -> None:
        """Download the model from the registry. Logs progress when stream=True. Raises on failure."""
        body = {"model": model_name, "stream": stream}
        req = urllib.request.Request(
            f"{self._base_url}/api/pull",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        logger.info("Downloading model %s (ollama)...", model_name)
        with urllib.request.urlopen(req, timeout=3600) as resp:
            if stream:
                last_pct = -1
                for line in resp:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    status = event.get("status", "")
                    total = event.get("total") or 0
                    completed = event.get("completed") or 0
                    if total and total > 0 and completed >= 0:
                        pct = min(100, int(100 * completed / total))
                        if pct != last_pct and (pct % 10 == 0 or pct == 100):
                            last_pct = pct
                            total_mb = total / (1024 * 1024)
                            done_mb = completed / (1024 * 1024)
                            bar_len = 10
                            filled = int(bar_len * pct / 100) if pct < 100 else bar_len
                            bar = "=" * filled + ">" * (1 if filled < bar_len and pct > 0 else 0) + " " * (bar_len - filled - (1 if filled < bar_len and pct > 0 else 0))
                            logger.info(
                                "Pulling model %s: [%s] %d%% (%.1f / %.1f MB)",
                                model_name, bar[:bar_len], pct, done_mb, total_mb,
                            )
                    elif status:
                        logger.debug("Pulling model %s: %s", model_name, status)
                logger.info("Model %s (ollama) pull complete.", model_name)
            else:
                json.loads(resp.read().decode())
                logger.info("Model %s (ollama) pull complete.", model_name)

    def delete_model(self, model_name: str) -> None:
        """Remove the model from the server. Raises on failure."""
        req = urllib.request.Request(
            f"{self._base_url}/api/delete",
            data=json.dumps({"model": model_name}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def ensure_model(self, model_name: str) -> bool:
        """
        Ensure the model is available: pull if not present.
        Returns True if we pulled the model (caller may want to remove it later), False if already present.
        Logs download start and progress (when streaming).
        """
        names = self.list_models()
        for n in names:
            if n == model_name or model_name in n or (model_name.split(":")[0] == n.split(":")[0] if ":" in n else n == model_name.split(":")[0]):
                return False
        self.pull_model(model_name, stream=True)
        return True

    def load_model(self, model_name: str, config: Optional[Dict[str, Any]] = None) -> None:
        """Load model into memory by running a minimal generate. Idempotent if already loaded."""
        self._generate(model_name, " ", num_predict=1, keep_alive=None)

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call Ollama /api/generate; map payload to prompt and parse response."""
        config = config or {}
        subtype = config.get("subtype") or ""
        model = config.get("model") or "llama3.2:3b"
        text = payload.get("text") or payload.get("content") or payload.get("url") or ""
        try:
            if subtype == "emotion_classification" or subtype == "emo_27":
                prompt = (
                    f'Classify the emotion of this text in one word or short phrase. '
                    f'Reply with JSON only: {{"emotion_label": "...", "confidence": 0.9}}\n\nText: {text}'
                )
            elif subtype == "topic_extraction":
                prompt = (
                    "Extract up to 5 topics from the text. Reply with JSON only: "
                    '{"topics": [{"label": "...", "confidence": 0.9}]}\n\nText: '
                    f"{text}"
                )
            elif subtype == "raw_to_summary":
                dimension = payload.get("dimension") or "memory"
                records = payload.get("records") or []
                context = "\n".join(str(r.get("content", r))[:500] for r in records[:20] if r)
                templates = _load_prompt_config().get("dimension_summary_templates") or {}
                template = templates.get(dimension) or f"Summarize the following {dimension} dimension records."
                prompt = f"{template}\n\nRecords:\n{context}\n\nReply JSON: {{\"summary_text\": \"...\", \"dimension\": \"{dimension}\"}}"
            elif subtype == "goal_extraction":
                prompt = (
                    "Extract user goals from the AI chat content. Reply JSON only: "
                    '{"goals": [{"text": "...", "confidence": 0.8, "horizon": "short"}]}\n\nText: '
                    f"{text}"
                )
            elif subtype == "query_inference":
                ctx = payload.get("context") or ""
                q = payload.get("query") or text
                prompt = (
                    f"Answer yes or no with confidence 0-1. Reply JSON only: "
                    f'{{"answer": "yes|no|unknown", "confidence": 0.5}}\n\nQuery: {q}\n\nContext: {ctx[:3500]}'
                )
            else:
                prompt = str(payload) if payload else ""
            response_text = self._generate(model, prompt, num_predict=None, keep_alive=None)
            out = self._parse_response(response_text, subtype, model)
            out["model"] = model
            return out
        except urllib.error.URLError:
            return {
                "status": "deferred",
                "error": "ollama_unreachable",
                "model": model,
            }
        except RuntimeError as exc:
            if "Ollama request failed" in str(exc):
                return {"status": "deferred", "error": "ollama_unreachable", "model": model}
            return {"error": str(exc), "model": model}
        except Exception as e:
            return {"error": str(e), "model": model, "emotion_label": None, "confidence": None, "all_emotions": []}

    def _generate(
        self,
        model: str,
        prompt: str,
        *,
        num_predict: Optional[int] = None,
        keep_alive: Optional[str] = None,
    ) -> str:
        body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        if num_predict is not None:
            body["options"] = {"num_predict": num_predict}
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
                return data.get("response", "")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e

    def _parse_response(self, response_text: str, subtype: str, model: str) -> Dict[str, Any]:
        """Try to parse JSON from response; else return raw."""
        response_text = (response_text or "").strip()
        if subtype in ("emotion_classification", "emo_27"):
            try:
                # Try to find JSON in the response
                start = response_text.find("{")
                if start >= 0:
                    end = response_text.rfind("}") + 1
                    if end > start:
                        obj = json.loads(response_text[start:end])
                        return {
                            "emotion_label": obj.get("emotion_label"),
                            "confidence": obj.get("confidence"),
                            "all_emotions": [{"label": obj.get("emotion_label"), "confidence": obj.get("confidence", 0)}],
                        }
            except (json.JSONDecodeError, KeyError):
                pass
            return {"emotion_label": response_text[:100] if response_text else None, "confidence": None, "all_emotions": []}
        if subtype == "topic_extraction":
            parsed = self._parse_json_object(response_text)
            topics = parsed.get("topics") if isinstance(parsed, dict) else []
            if not isinstance(topics, list):
                topics = []
            return {"topics": topics[:5], "model": model}
        if subtype == "raw_to_summary":
            parsed = self._parse_json_object(response_text)
            if isinstance(parsed, dict) and parsed.get("summary_text"):
                return {"summary_text": parsed.get("summary_text"), "dimension": parsed.get("dimension"), "model": model}
            return {"summary_text": response_text[:2000], "dimension": "memory", "model": model}
        if subtype == "goal_extraction":
            parsed = self._parse_json_object(response_text)
            goals = parsed.get("goals") if isinstance(parsed, dict) else []
            if not isinstance(goals, list):
                goals = []
            return {"goals": goals, "model": model}
        if subtype == "query_inference":
            parsed = self._parse_json_object(response_text)
            if isinstance(parsed, dict):
                return {
                    "answer": parsed.get("answer"),
                    "confidence": parsed.get("confidence"),
                    "model": model,
                }
            return {"answer": "unknown", "confidence": 0.0, "model": model}
        return {"output": response_text}

    def _parse_json_object(self, response_text: str) -> Dict[str, Any]:
        response_text = (response_text or "").strip()
        start = response_text.find("{")
        if start < 0:
            return {}
        end = response_text.rfind("}") + 1
        if end <= start:
            return {}
        try:
            obj = json.loads(response_text[start:end])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}

    def unload_model(self, model_name: str) -> None:
        """Unload model from memory by sending a minimal generate with keep_alive=0."""
        self._generate(model_name, " ", num_predict=1, keep_alive="0")
