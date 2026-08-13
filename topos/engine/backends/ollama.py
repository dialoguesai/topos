"""Ollama backend adapter: HTTP API for local LLM inference."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .generative_prompts import build_generative_prompt
from .generative_response import parse_generative_response

logger = logging.getLogger("topos.engine.ollama")

# Per-process thinking-capability cache, keyed by (base_url, model). Populated
# lazily from /api/show ("capabilities" contains "thinking"). Module-level so
# every OllamaAdapter instance (they are cheap and constructed per call site)
# shares one probe per model.
_THINKING_CAPABILITY_CACHE: Dict[tuple, bool] = {}
# Models that support thinking but REJECT think=false (e.g. gpt-oss family:
# 'does not support disabling thinking'). For these we omit the param and
# instead widen num_predict so the chain-of-thought cannot starve the answer.
_THINK_DISABLE_REJECTED: set = set()
# When a thinking model cannot disable thinking, its CoT competes with the
# answer for output budget; widen num_predict so the JSON still fits.
_THINKING_NUM_PREDICT_FLOOR = 2048

_STRUCTURED_SUBTYPES = frozenset(
    {
        "topic_extraction",
        "brief_update",
        "raw_to_summary",
        "goal_extraction",
        "query_inference",
        "truth_verdict",
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
    if subtype == "truth_verdict":
        # Small strict-JSON verdict; a tight cap keeps the nose responsive.
        return 256
    return None


def _temperature_for_subtype(subtype: str) -> Optional[float]:
    """Query inference must be deterministic: at default temperature llama3.2
    flips yes↔unknown on identical score-only evidence packets."""
    if subtype in ("query_inference", "truth_verdict"):
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

    def model_supports_thinking(self, model: str) -> bool:
        """True when /api/show lists the 'thinking' capability for ``model``.

        Cached per (base_url, model) for the process lifetime. A failed probe
        is NOT cached (transient hiccups retry on the next call) and returns
        False — the caller then omits ``think``, which is always accepted.
        """
        key = (self._base_url, str(model))
        cached = _THINKING_CAPABILITY_CACHE.get(key)
        if cached is not None:
            return cached
        req = urllib.request.Request(
            f"{self._base_url}/api/show",
            data=json.dumps({"model": str(model)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 — degrade to "unknown, omit think"
            logger.debug("thinking-capability probe failed for %s: %s", model, exc)
            return False
        supports = "thinking" in (data.get("capabilities") or [])
        _THINKING_CAPABILITY_CACHE[key] = supports
        logger.info(
            "Ollama model %s thinking capability: %s", model, supports
        )
        return supports

    def resolve_think(self, model: str, desired: Optional[bool]) -> Optional[bool]:
        """Adapt a desired ``think`` flag to what ``model`` actually accepts.

        - desired None → None (caller doesn't care; model default applies).
        - model without the thinking capability → None (omit the param; some
          Ollama versions 400 on it, and it is meaningless anyway).
        - model that rejected think=false before → None (see _generate, which
          widens num_predict for these instead).
        - otherwise → desired.
        """
        if desired is None:
            return None
        key = (self._base_url, str(model))
        if desired is False and key in _THINK_DISABLE_REJECTED:
            return None
        if not self.model_supports_thinking(model):
            return None
        return desired

    def list_models(self) -> List[str]:
        """Return list of model names available on the server (from /api/tags)."""
        return [m["name"] for m in self.list_models_detailed() if m.get("name")]

    def list_models_detailed(self) -> List[Dict[str, Any]]:
        """Installed tags with size / capabilities / modified_at from /api/tags.

        Size is what a size-labelled CTA needs for an already-installed tag
        (Branch A). Capabilities drive tools-preferring preselect and chips;
        modified_at is the recency fallback. Uninstalled starters still use the
        curated table — Ollama has no size for a tag that is not on disk
        (PLAN_LOCAL_MODEL_QUICKSTART D4).
        """
        req = urllib.request.Request(f"{self._base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                out: List[Dict[str, Any]] = []
                for m in data.get("models", []) or []:
                    name = m.get("name")
                    if not name:
                        continue
                    entry: Dict[str, Any] = {"name": str(name)}
                    raw_size = m.get("size")
                    if isinstance(raw_size, int) and raw_size >= 0:
                        entry["size"] = raw_size
                    raw_caps = m.get("capabilities")
                    if isinstance(raw_caps, list):
                        caps = [str(c).strip() for c in raw_caps if str(c or "").strip()]
                        if caps:
                            entry["capabilities"] = caps
                    modified = m.get("modified_at") or m.get("modifiedAt")
                    if isinstance(modified, str) and modified.strip():
                        entry["modified_at"] = modified.strip()
                    out.append(entry)
                return out
        except Exception:
            return []

    def pull_model(
        self,
        model_name: str,
        *,
        stream: bool = True,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """Download the model from the registry. Logs progress when stream=True. Raises on failure.

        ``on_progress`` receives each decoded /api/pull frame so a caller can
        show the download to a human. A callback that raises must not abort a
        multi-gigabyte transfer that is otherwise healthy.
        """
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
                    if on_progress is not None:
                        try:
                            on_progress(event)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("pull progress callback failed: %s", exc)
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
            # A pack's binding beats the per-subtype default: the default is a
            # code-shipped guess about a subtype, the binding is the owner
            # stating it about this model. An absent key keeps the guess.
            generated = self._generate(
                model,
                prompt,
                num_predict=config.get("max_tokens") or _num_predict_for_subtype(subtype),
                keep_alive=None,
                think=config.get("thinking", _think_for_subtype(subtype)),
                temperature=_temperature_for_subtype(subtype),
                num_ctx=config.get("context"),
            )
            response_text = str(generated.get("text") or "")
            out = parse_generative_response(response_text, subtype, model, payload=payload)
            out["model"] = model
            usage = generated.get("usage")
            if isinstance(usage, dict):
                out["usage"] = usage
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
        num_ctx: Optional[int] = None,
        timeout: float = 300,
    ) -> Dict[str, Any]:
        # think auto-adaptation: callers state intent (False = suppress
        # chain-of-thought for structured output); what is actually sent depends
        # on the model. Non-thinking models get the param omitted; models that
        # rejected think=false before keep thinking but get a wider output
        # budget so the CoT cannot starve the answer (Ollama returns thinking
        # in a separate field, so 'response' stays clean either way).
        requested_think = think
        think = self.resolve_think(model, think)
        if (
            requested_think is False
            and think is None
            and (self._base_url, str(model)) in _THINK_DISABLE_REJECTED
            and num_predict is not None
        ):
            num_predict = max(num_predict, _THINKING_NUM_PREDICT_FLOOR)
        try:
            return self._generate_once(
                model,
                prompt,
                num_predict=num_predict,
                keep_alive=keep_alive,
                think=think,
                temperature=temperature,
                num_ctx=num_ctx,
                timeout=timeout,
            )
        except RuntimeError as exc:
            # First contact with a gpt-oss-style model: thinking capability is
            # advertised but cannot be turned off. Remember, widen, retry once.
            if think is False and "disabling thinking" in str(exc).lower():
                _THINK_DISABLE_REJECTED.add((self._base_url, str(model)))
                logger.info(
                    "Ollama model %s rejects think=false; retrying with thinking "
                    "enabled and num_predict floor %d",
                    model,
                    _THINKING_NUM_PREDICT_FLOOR,
                )
                return self._generate_once(
                    model,
                    prompt,
                    num_predict=(
                        max(num_predict, _THINKING_NUM_PREDICT_FLOOR)
                        if num_predict is not None
                        else None
                    ),
                    keep_alive=keep_alive,
                    think=None,
                    temperature=temperature,
                    num_ctx=num_ctx,
                    timeout=timeout,
                )
            raise

    def _generate_once(
        self,
        model: str,
        prompt: str,
        *,
        num_predict: Optional[int],
        keep_alive: Optional[str],
        think: Optional[bool],
        temperature: Optional[float],
        num_ctx: Optional[int],
        timeout: float,
    ) -> Dict[str, Any]:
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
        if num_ctx is not None:
            options["num_ctx"] = num_ctx
        if options:
            body["options"] = options
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                prompt_tokens = int(data.get("prompt_eval_count") or 0)
                completion_tokens = int(data.get("eval_count") or 0)
                total_tokens = prompt_tokens + completion_tokens
                return {
                    "text": data.get("response", ""),
                    "usage": {
                        "prompt_tokens": max(0, prompt_tokens),
                        "completion_tokens": max(0, completion_tokens),
                        "total_tokens": max(0, total_tokens),
                    },
                }
        except urllib.error.HTTPError as e:
            # Include the response body: Ollama puts the actionable message
            # there (e.g. '"x" does not support disabling thinking') and the
            # bare status line hides it.
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            raise RuntimeError(f"Ollama request failed: {e} {detail}".rstrip()) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e

    def unload_model(self, model_name: str) -> None:
        """Unload model from memory by sending a minimal generate with keep_alive=0."""
        self._generate(model_name, " ", num_predict=1, keep_alive="0")
