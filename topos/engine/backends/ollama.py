"""Ollama backend adapter: HTTP API for local LLM inference."""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.engine.ollama")


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
            else:
                prompt = str(payload) if payload else ""
            response_text = self._generate(model, prompt, num_predict=None, keep_alive=None)
            out = self._parse_response(response_text, subtype, model)
            out["model"] = model
            return out
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
        return {"output": response_text}

    def unload_model(self, model_name: str) -> None:
        """Unload model from memory by sending a minimal generate with keep_alive=0."""
        self._generate(model_name, " ", num_predict=1, keep_alive="0")
