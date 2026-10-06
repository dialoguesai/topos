"""Pinned local model and rubric used by the sharing review and answer paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# The campaign's own local binding (`run_nl_experiment.py`): the same tag and reviewed digest, so a verdict here is
# attributable to the same model the program has already measured. `ORIGIN` is the campaign's loopback host, and
# this labeler's host only when the node has not set `ENGINE_OLLAMA_BASE_URL` (`configured_base_url`).
ORIGIN = "http://127.0.0.1:11434"
# The setting the engine's own adapter reads (`engine/backends/ollama.py`, `cli/setup_models_cmd.py`): the
# environment's `ENGINE_OLLAMA_BASE_URL`.
BASE_URL_SETTING = "engine_ollama_base_url"
MODEL = "qwen3.5:9b-mlx"
MODEL_REVISION = "203e30078279db51132b9e026ceb7bb21330e5b1af67ef190671b375c9770404"
TIMEOUT_SECONDS = 25
MAX_TEXT_CHARS = 8_000

RUBRIC_PATH = Path(__file__).with_name("shadow_rubric.pinned.md")
RUBRIC_SHA256 = "d03b3358357cc44f85976a5eb3e80840158b701fc4e30d0e4ec5a819c959701d"
RUBRIC_BYTES = 1702
# The rubric's own closed vocabulary. Restated here so a labeler answer is checked against a set this module owns
# rather than against whatever the prose happens to contain; `test_L1` ties the two together.
DOMAINS = ("work", "plans", "hobbies", "health", "family", "finance", "relationships", "home")
SENSITIVITIES = ("none", "personal", "special")

PROMPT_VERSION = "topos-shadow-label-prompt/v1"
TEMPLATE = (
    "You label one message against a fixed rubric. Answer with JSON only, exactly "
    '{"domains": [...], "sensitivity": "..."} and nothing else.\n'
    "`domains` is every domain the message touches, from the rubric's list, possibly empty. "
    "`sensitivity` is exactly one value, the highest that applies.\n"
    "Use only the values the rubric defines. Do not explain. Do not add fields.\n\n")


class Unresolved(Exception):
    """A release this labeler cannot resolve, and why: a code in the control plane's reason grammar, never text."""

    reason = "labeler_unresolved"


class Unreachable(Unresolved):
    """The host could not be asked, or did not deliver a completed answer: no connection, a timeout, an error
    status, a reply that is not a finished generation. The model has not been heard from."""

    reason = "labeler_unreachable"


class ModelUnreviewed(Unresolved, ValueError):
    """The installed tag is not at the reviewed digest, or the model that answered is not the pinned one."""

    reason = "labeler_model_unreviewed"


class RubricMismatch(Unresolved, ValueError):
    """The pinned rubric on disk is not the reviewed bytes, or is not there."""

    reason = "labeler_rubric_mismatch"


def rubric() -> str:
    try:
        raw = RUBRIC_PATH.read_bytes()
    except OSError as exc:
        raise RubricMismatch("the pinned rubric is not there") from exc
    if len(raw) != RUBRIC_BYTES or hashlib.sha256(raw).hexdigest() != RUBRIC_SHA256:
        raise RubricMismatch("the pinned rubric is not the reviewed one")
    return raw.decode("utf-8")


def system_prompt() -> str:
    return TEMPLATE + rubric()


def configured_base_url(settings=None) -> str:
    """Where this node talks to Ollama: `ENGINE_OLLAMA_BASE_URL` as the engine's adapter reads it, else `ORIGIN`.

    Read through `settings.engine_ollama_base_url`, the setting `OllamaAdapter` and `setup-models` read, so the
    labeler asks the model host the node actually has. Only a value the node set counts: the setting's own default
    is the adapter's spelling of the same loopback, not a configuration, and a node that set nothing, set it blank,
    or whose settings will not load keeps the campaign's binding, `ORIGIN`. Resolving the host starts, opens and
    pulls nothing; the transport is the only thing that talks to it.
    """
    if settings is None:
        try:
            from topos.config.settings import settings as loaded
        except Exception:  # noqa: BLE001 -- settings that will not load are a node that has set nothing
            return ORIGIN
        settings = loaded
    try:
        if BASE_URL_SETTING not in getattr(settings, "model_fields_set", ()):
            return ORIGIN
        value = str(getattr(settings, BASE_URL_SETTING, None) or "").strip()
    except Exception:  # noqa: BLE001
        return ORIGIN
    return (value or ORIGIN).rstrip("/")


def assessment_base_url(settings=None) -> str:
    """Where a sharing assessment asks its model (BL-15): the node's configured host when it is this machine, else
    ``ORIGIN``.

    A machine assessment, an interest label, its second try and the native message ceiling send the owner's own
    words and the Off-limits terms to the model. Until BL-15 they always asked the fixed loopback, so a node whose
    model listens on another local address or port could never assess. They now ask the configured host, but only
    when it is this machine (``engine.ollama_runtime.is_local_base_url``): a configured remote host never receives
    that context (``test_automatic_review_cannot_send_protected_context_to_configured_remote_model``), and those calls
    keep the loopback, as before. The shadow labeler, which sends one released record, keeps ``configured_base_url``.
    """
    from topos.engine.ollama_runtime import is_local_base_url
    configured = configured_base_url(settings)
    return configured if is_local_base_url(configured) else ORIGIN


def parse_labels(raw) -> dict | None:
    """The model's answer as rubric labels, or None. Nothing is coerced and nothing is dropped."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(raw, dict) or set(raw) != {"domains", "sensitivity"}:
        return None
    domains, sensitivity = raw["domains"], raw["sensitivity"]
    if not isinstance(domains, list) or any(not isinstance(item, str) for item in domains):
        return None
    if len(set(domains)) != len(domains) or any(item not in DOMAINS for item in domains):
        return None
    if not isinstance(sensitivity, str) or sensitivity not in SENSITIVITIES:
        return None
    return {"domains": list(domains), "sensitivity": sensitivity}


class _PinnedTransport:
    """One Ollama call per record, at the node's configured host, against the reviewed tag and digest.

    Never a pull, never a fallback, never a launch: a host that does not answer is `Unreachable`, and nothing here
    asks the engine's runtime to start one.
    """

    def __init__(self, client, base_url: str | None = None):
        self.client = client
        self.base_url = str(base_url or configured_base_url()).rstrip("/")
        self.calls = 0

    async def verify(self) -> None:
        try:
            response = await self.client.get(self.base_url + "/api/tags", timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            models = response.json().get("models") or []
        except Exception as exc:  # noqa: BLE001 -- whatever the host did, it has not listed its models
            raise Unreachable("the local model host did not answer") from exc
        installed = [row for row in models if isinstance(row, dict) and row.get("name") == MODEL]
        if len(installed) != 1 or installed[0].get("digest") != MODEL_REVISION:
            raise ModelUnreviewed("the installed local model is not the reviewed revision")

    async def label(self, text: str):
        await self.verify()
        # Built before the request: a rubric that is not the reviewed one is its own reason, not the host's.
        prompt = system_prompt()
        self.calls += 1
        try:
            response = await self.client.post(self.base_url + "/api/chat", timeout=TIMEOUT_SECONDS, json={
                "model": MODEL, "stream": False, "think": False, "format": "json",
                "messages": [{"role": "system", "content": prompt},
                             {"role": "user", "content": text[:MAX_TEXT_CHARS]}]})
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise Unreachable("the local model did not answer") from exc
        if not isinstance(body, dict):
            raise Unreachable("the local model did not answer")
        if body.get("model") != MODEL:
            raise ModelUnreviewed("the model that answered is not the reviewed one")
        if body.get("done") is not True:
            raise Unreachable("the local model did not complete")
        return (body.get("message") or {}).get("content")


def open_transport(base_url: str | None = None):
    """The pinned client at the node's configured host. `trust_env=False`, so no proxy in the environment can move
    these bytes."""
    import httpx
    return _PinnedTransport(httpx.AsyncClient(trust_env=False, follow_redirects=False), base_url=base_url)
