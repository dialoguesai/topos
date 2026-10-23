"""Structured model calls for verify_claim v2: the reader and the per-record judge.

The node writes these prompts; the control plane writes none. Two backends, chosen by the owner's setting
(``TOPOS_VERIFY_V2_MODEL`` = ``hosted`` | ``local``; default hosted when a Red Pill key is configured):

- ``HostedChat``: Red Pill (Topos Secure) with the node's own ``red_pill_api_key`` / ``red_pill_api_base``,
  behind the same hosted-model wallet check as the enrichment adapter. Schema-constrained JSON output.
- ``LocalChat``: the local Ollama server, schema-constrained via ``format``.

Each call returns the parsed JSON object and adds token usage to a ``Usage``. Any transport, HTTP, parse or wallet
failure raises ``ModelUnavailable``; the pipeline turns that into a refusal, never a guess.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

DEFAULT_HOSTED_MODEL = "meta-llama/llama-3.3-70b-instruct"
#: An optional second hosted judge: the owner's Topos Secure replacement for the unrouted gpt-oss-20b (OD-55,
#: 30 Sep 2026). Off unless ``TOPOS_VERIFY_V2_SECOND_JUDGE`` names it: on the owner's development rows it added true
#: and false support at about the same rate and cost ~2 s of judge time.
SECOND_JUDGE_MODEL = "openai/gpt-oss-120b"
#: Hosted models verify may name directly; anything else goes through the enrichment adapter's own allowlist.
VERIFY_HOSTED_MODELS = frozenset({DEFAULT_HOSTED_MODEL, SECOND_JUDGE_MODEL})
DEFAULT_LOCAL_MODEL = "qwen3.5:9b-mlx"
SEED = 7


class ModelUnavailable(Exception):
    """The model could not answer in time or in shape; the caller refuses."""


class OutputSchema(dict):
    """A JSON schema plus how to ask for it. ``plain_json`` asks for a JSON object with the schema in the prompt instead
    of a strict grammar: Red Pill's grammar mode made llama stream whitespace after "{" for a short list schema (30 s
    timeouts on most real rows, 23 Oct 2026) where plain JSON answered in ~0.6 s. ``max_tokens`` caps the reply so a
    degenerate stream fails fast (unparseable -> unable) instead of running to the request deadline."""

    def __init__(self, schema: Dict[str, Any], *, plain_json: bool = False, max_tokens: Optional[int] = None):
        super().__init__(schema)
        self.plain_json, self.max_tokens = bool(plain_json), max_tokens


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0

    def add(self, prompt: Any, output: Any) -> None:
        self.calls += 1
        self.prompt_tokens += int(prompt or 0)
        self.output_tokens += int(output or 0)


def _post_json(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    if timeout <= 0:
        raise ModelUnavailable("deadline")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ModelUnavailable(f"http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ModelUnavailable(type(exc).__name__) from None


def _parse(content: Any) -> Dict[str, Any]:
    text = str(content or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        out = json.loads(text)
    except ValueError:
        raise ModelUnavailable("unparseable") from None
    if not isinstance(out, dict):
        raise ModelUnavailable("not_an_object")
    return out


class HostedChat:
    """Red Pill chat completions with a JSON schema. Uses the node's configured key; never logs it."""

    name = "hosted"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 options: Optional[Dict[str, Any]] = None):
        self._options = dict(options or {})  # extra request fields, e.g. {"reasoning_effort": "low"}
        if api_key is None or base_url is None:
            from ..config.settings import settings

            api_key = api_key if api_key is not None else str(getattr(settings, "red_pill_api_key", None) or "")
            base_url = base_url or str(getattr(settings, "red_pill_api_base", None) or "https://api.redpill.ai/v1")
        from ..engine.backends.redpill import resolve_redpill_model

        self.model = model if model in VERIFY_HOSTED_MODELS else resolve_redpill_model(model or DEFAULT_HOSTED_MODEL)
        self._key, self._base = api_key.strip(), base_url.rstrip("/")
        self._schema_mode = True

    @property
    def available(self) -> bool:
        return bool(self._key)

    def _wallet_allows(self) -> bool:
        try:
            from ..engine.hosted_llm_wallet import hosted_llm_wallet_allows

            return bool(hosted_llm_wallet_allows())
        except Exception:  # noqa: BLE001 — a probe failure must not block a configured key
            return True

    def __call__(self, system: str, user: str, schema: Dict[str, Any], timeout: float, usage: Usage) -> Dict[str, Any]:
        if not self._key:
            raise ModelUnavailable("no_hosted_key")
        if not self._wallet_allows():
            raise ModelUnavailable("insufficient_credits")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body: Dict[str, Any] = {"model": self.model, "temperature": 0, "seed": SEED, "messages": messages, **self._options}
        cap = getattr(schema, "max_tokens", None)
        if cap:
            body["max_tokens"] = int(cap)
        if self._schema_mode and not getattr(schema, "plain_json", False):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        else:
            body["messages"] = [{"role": "system", "content": system + "\n\nReply with ONLY a JSON object matching this "
                                 "JSON schema: " + json.dumps(schema)}, messages[1]]
            body["response_format"] = {"type": "json_object"}
        try:
            reply = _post_json(self._base + "/chat/completions", body, {"Authorization": f"Bearer {self._key}"}, timeout)
        except ModelUnavailable as exc:
            if str(exc) == "http_400" and self._schema_mode:
                self._schema_mode = False  # provider/model without schema-constrained output
                return self(system, user, schema, timeout, usage)
            raise
        u = reply.get("usage") or {}
        usage.add(u.get("prompt_tokens"), u.get("completion_tokens"))
        try:
            return _parse(reply["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise ModelUnavailable("bad_reply") from None


class LocalChat:
    """Local Ollama /api/chat with the schema as ``format``."""

    name = "local"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self.model = model or os.environ.get("TOPOS_VERIFY_V2_LOCAL_MODEL") or DEFAULT_LOCAL_MODEL
        self._base = (base_url or os.environ.get("ENGINE_OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")

    available = True

    def __call__(self, system: str, user: str, schema: Dict[str, Any], timeout: float, usage: Usage) -> Dict[str, Any]:
        body = {"model": self.model, "stream": False, "format": dict(schema), "think": False,
                "options": {"temperature": 0, "seed": SEED},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        cap = getattr(schema, "max_tokens", None)
        if cap:
            body["options"]["num_predict"] = int(cap)
        reply = _post_json(self._base + "/api/chat", body, {}, timeout)
        usage.add(reply.get("prompt_eval_count"), reply.get("eval_count"))
        try:
            return _parse(reply["message"]["content"])
        except (KeyError, TypeError):
            raise ModelUnavailable("bad_reply") from None


def judges_for_setting() -> list:
    """The judges for the owner's setting: the reader's own model, plus a second hosted model when one is configured."""
    reader = chat_for_setting()
    if not isinstance(reader, HostedChat):
        return [reader]
    second = os.environ.get("TOPOS_VERIFY_V2_SECOND_JUDGE", "").strip()
    return [reader] + ([HostedChat(model=second)] if second and second != reader.model else [])


def chat_for_setting() -> Any:
    """The owner's choice: hosted (default when a key is configured) or local."""
    choice = (os.environ.get("TOPOS_VERIFY_V2_MODEL") or "").strip().lower()
    if choice == "local":
        return LocalChat()
    hosted = HostedChat()
    if choice == "hosted" or hosted.available:
        return hosted
    return LocalChat()
