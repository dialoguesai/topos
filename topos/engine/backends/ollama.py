"""Ollama backend adapter: HTTP API for local LLM inference."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .generative_prompts import build_generative_prompt
from .generative_response import parse_generative_response

logger = logging.getLogger("topos.engine.ollama")

_STRUCTURED_SUBTYPES = frozenset(
    {
        "topic_extraction",
        "brief_update",
        "raw_to_summary",
        "goal_extraction",
        "query_inference",
        "emotion_classification",
        "emo_27",
    }
)


def _think_for_subtype(subtype: str) -> Optional[bool]:
    """Reasoning models (e.g. qwen3.5) spend minutes in chain-of-thought unless disabled."""
    if subtype in _STRUCTURED_SUBTYPES:
        return False
    return None


def _num_predict_for_subtype(subtype: str) -> Optional[int]:
    if subtype == "topic_extraction":
        return 256
    if subtype in ("brief_update", "raw_to_summary"):
        return 2048
    if subtype == "goal_extraction":
        return 512
    return None


def _temperature_for_subtype(subtype: str) -> Optional[float]:
    """Query inference must be deterministic: at default temperature llama3.2
    flips yes↔unknown on identical score-only evidence packets."""
    if subtype == "query_inference":
        return 0.0
    return None


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

    def is_reachable(self, *, timeout: float = 2.0) -> bool:
        """True when the Ollama server answers /api/tags (fast health probe)."""
        req = urllib.request.Request(f"{self._base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:
            return False

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
        try:
            prompt = build_generative_prompt(subtype, payload)
            response_text = self._generate(
                model,
                prompt,
                num_predict=_num_predict_for_subtype(subtype),
                keep_alive=None,
                think=_think_for_subtype(subtype),
                temperature=_temperature_for_subtype(subtype),
            )
            out = parse_generative_response(response_text, subtype, model, payload=payload)
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
        think: Optional[bool] = None,
        temperature: Optional[float] = None,
    ) -> str:
        body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        if think is not None:
            body["think"] = think
        options: Dict[str, Any] = {}
        if num_predict is not None:
            options["num_predict"] = num_predict
        if temperature is not None:
            options["temperature"] = temperature
        if options:
            body["options"] = options
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
                return data.get("response", "")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e

    def unload_model(self, model_name: str) -> None:
        """Unload model from memory by sending a minimal generate with keep_alive=0."""
        self._generate(model_name, " ", num_predict=1, keep_alive="0")
