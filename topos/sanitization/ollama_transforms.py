"""
v1 LLM-backed field transforms via Ollama (e.g. llama3.2).

Configuration (precedence: device DB overrides → Settings / env → safe defaults):
  - File/env: see `topos.config.settings` (SANITIZATION_OLLAMA_*).
  - Device: `engine_config` key `sanitization_ollama_device` (JSON), via PUT /v1/sanitization-ollama-config.

If /api/chat returns 404 with a model-not-found error and SANITIZATION_OLLAMA_AUTO_PULL is true (default),
the engine pulls the model via Ollama /api/pull (see OllamaAdapter.ensure_model) then retries chat once.

Fail-open: on error, callers keep the original text.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Final, Iterator, List, Optional, Tuple

from topos.config.sanitization_ollama import (
    SANITIZATION_OLLAMA_TRANSFORM_IDS,
    SanitizationOllamaEffective,
    resolve_sanitization_ollama_effective,
)

logger = logging.getLogger("topos.sanitization.ollama")

# Serialize Ollama pulls when many UMA rows hit a missing model at once.
_pull_locks: Dict[Tuple[str, str], threading.Lock] = {}
_pull_registry_lock = threading.Lock()


@contextmanager
def _serialized_ollama_pull(host: str, model: str) -> Iterator[None]:
    key = (host.rstrip("/"), model)
    with _pull_registry_lock:
        if key not in _pull_locks:
            _pull_locks[key] = threading.Lock()
        lock = _pull_locks[key]
    with lock:
        yield


def _response_suggests_missing_ollama_model(response: Any) -> bool:
    """Heuristic: Ollama /api/chat 404 JSON error for an unknown local model tag."""
    if getattr(response, "status_code", None) != 404:
        return False
    err = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            err = str(data.get("error") or "")
    except Exception:
        err = str(getattr(response, "text", None) or "")
    el = err.lower()
    return "not found" in el and "model" in el

# Backward-compatible alias
OLLAMA_TRANSFORM_IDS: Final[tuple[str, ...]] = SANITIZATION_OLLAMA_TRANSFORM_IDS

_SYSTEM_STRICT: Final[str] = (
    "You are a precise text processor for a privacy-preserving data export pipeline. "
    "Follow instructions exactly. Output ONLY the requested result with no markdown fences, "
    "no preamble, and no explanation unless the format explicitly asks for structured fields."
)


def _user_pii_redaction(text: str) -> str:
    return f"""Redact personally identifiable information in the following text.
Replace each span of PII with a bracketed token: [NAME], [EMAIL], [PHONE], [ADDRESS], [ID], [URL], [DATE_OF_BIRTH], [ACCOUNT], [OTHER_PII].
Preserve structure (paragraphs, newlines) and non-PII wording. Do not invent content.

TEXT:
---
{text}
---
OUTPUT (redacted text only):"""


def _user_nsfw_sanitization(text: str) -> str:
    return f"""Sanitize the following text for a general audience: remove or replace sexually explicit, graphically violent, or illegal-content descriptions with [REMOVED].
Keep the rest of the message meaning where possible. Do not add commentary.

TEXT:
---
{text}
---
OUTPUT (sanitized text only):"""


def _user_raw_to_summary(text: str, params: Dict[str, Any]) -> str:
    style = str(params.get("style") or "neutral").strip() or "neutral"
    max_len = params.get("max_length")
    cap = ""
    if isinstance(max_len, int) and max_len > 0:
        cap = f" Maximum length: about {max_len} words."
    return f"""Summarize the following text in a {style} tone.{cap}
Output a single concise paragraph.

TEXT:
---
{text}
---
OUTPUT (summary only):"""


def _user_raw_to_sentiment(text: str, params: Dict[str, Any]) -> str:
    scale = str(params.get("scale") or "ternary").strip() or "ternary"
    return f"""Classify the overall sentiment of the text on scale "{scale}".
Respond with EXACTLY one line of JSON (no markdown) with keys: "label" (string) and "confidence" (number 0-1).
Example: {{"label":"positive","confidence":0.71}}

TEXT:
---
{text}
---
OUTPUT (one JSON line only):"""


def _user_third_party_anonymization(text: str, params: Dict[str, Any]) -> str:
    mode = str(params.get("mode") or "replace").strip() or "replace"
    return f"""Anonymize third-party identities in the text (other people, companies, or clients mentioned by name).
Mode: {mode}. Replace identifiable third-party names with [PERSON_1], [ORG_1], etc. Keep the narrator's own voice and first-person references unchanged if clearly "I/me/my".
Do not remove non-identifying roles ("my therapist", "a colleague") unless they include a name.

TEXT:
---
{text}
---
OUTPUT (anonymized text only):"""


def _user_name_removal(text: str) -> str:
    return f"""Remove personal names (people and well-known public figures) from the text. Replace each removed name with [NAME].
Keep dates, numbers, and non-name entities. Preserve readability.

TEXT:
---
{text}
---
OUTPUT (text only):"""


def _user_contact_removal(text: str) -> str:
    return f"""Remove contact details: email addresses, phone numbers, street addresses, social handles (@user), and messaging IDs.
Replace each removed span with [CONTACT]. Keep the rest of the message.

TEXT:
---
{text}
---
OUTPUT (text only):"""


def _build_messages(transform_id: str, text: str, params: Dict[str, Any]) -> List[Dict[str, str]]:
    params = params or {}
    builders = {
        "pii_redaction": lambda: _user_pii_redaction(text),
        "nsfw_sanitization": lambda: _user_nsfw_sanitization(text),
        "raw_to_summary": lambda: _user_raw_to_summary(text, params),
        "raw_to_sentiment": lambda: _user_raw_to_sentiment(text, params),
        "third_party_anonymization": lambda: _user_third_party_anonymization(text, params),
        "name_removal": lambda: _user_name_removal(text),
        "contact_removal": lambda: _user_contact_removal(text),
    }
    fn = builders.get(transform_id)
    if fn is None:
        raise ValueError(f"No Ollama prompt builder for transform_id={transform_id!r}")
    user_content = fn()
    return [
        {"role": "system", "content": _SYSTEM_STRICT},
        {"role": "user", "content": user_content},
    ]


def ollama_sanitization_enabled() -> bool:
    """Whether sanitization is enabled (Settings + device overrides)."""
    from topos.config.settings import settings
    from topos.core.state import get_db_connection

    return resolve_sanitization_ollama_effective(settings, get_db_connection()).enabled


def _ollama_chat(
    messages: List[Dict[str, str]],
    *,
    host: str,
    model: str,
    timeout_sec: float,
    auto_pull: bool = True,
    think: Optional[bool] = None,
    num_ctx: Optional[int] = None,
    num_predict: Optional[int] = None,
) -> str:
    import httpx

    url = f"{host.rstrip('/')}/api/chat"
    body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    # Omitted rather than defaulted when unset: sanitization ran without any of
    # these for its whole life, and sending Ollama's own defaults explicitly
    # would change every existing install's behaviour to prove a pack works.
    if think is not None:
        body["think"] = think
    options: Dict[str, Any] = {}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    if num_predict is not None:
        options["num_predict"] = num_predict
    if options:
        body["options"] = options
    with httpx.Client(timeout=timeout_sec) as client:
        resp = client.post(url, json=body)
        if (
            auto_pull
            and resp.status_code == 404
            and _response_suggests_missing_ollama_model(resp)
        ):
            logger.info(
                "Ollama model %r not available at %s; pulling via /api/pull then retrying /api/chat",
                model,
                host.rstrip("/"),
            )
            with _serialized_ollama_pull(host, model):
                from topos.engine.backends.ollama import OllamaAdapter

                pulled = OllamaAdapter(base_url=host).ensure_model(model)
                if pulled:
                    logger.info("Sanitization: finished pulling Ollama model %r", model)
            resp = client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise ValueError("Ollama response missing message.content")
    return content.strip()


def _strip_fences(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:\w+)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def apply_text_transform_with_ollama(
    text: str,
    transform_id: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    effective: SanitizationOllamaEffective,
    model_override: Optional[str] = None,
) -> str:
    """
    Run a single catalog transform on string `text` via Ollama.
    `effective` should be from `resolve_sanitization_ollama_effective` (caller resolves once per batch).
    `model_override`: when set (e.g. Filter Lab compare), use this Ollama tag instead of per-transform model.
    """
    if transform_id not in SANITIZATION_OLLAMA_TRANSFORM_IDS:
        raise ValueError(f"transform_id {transform_id!r} is not handled by Ollama sanitization")

    model = (model_override or "").strip() or effective.models.get(transform_id) or effective.default_model
    max_in = effective.max_input_chars
    if max_in > 0 and len(text) > max_in:
        text = text[:max_in] + "\n[TRUNCATED_FOR_LLM]"

    # Only the pack binding that names THIS model, so a device override or a
    # per-transform model does not inherit knobs set for a different one.
    pack = effective.params_for(model)

    messages = _build_messages(transform_id, text, dict(params or {}))
    raw = _ollama_chat(
        messages,
        host=effective.host,
        model=model,
        timeout_sec=effective.timeout_sec,
        auto_pull=effective.auto_pull,
        think=pack.thinking if pack else None,
        num_ctx=pack.context if pack else None,
        num_predict=pack.max_tokens if pack else None,
    )
    out = _strip_fences(raw)

    if transform_id == "raw_to_sentiment":
        try:
            line = out.splitlines()[0].strip()
            parsed = json.loads(line)
            label = str(parsed.get("label", "unknown"))
            conf = parsed.get("confidence")
            if isinstance(conf, (int, float)):
                return f"{label} ({float(conf):.2f})"
            return label
        except Exception:
            return out[:500]

    return out
