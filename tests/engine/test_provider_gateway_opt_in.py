"""The provider gateway is a separate program, and these are the guards that keep it one.

Three things are pinned here, each with controlled transports only (no model, no
real socket, no owner data):

* Opt-in. Nothing in the node imports, starts, configures or installs a command
  for ``topos.compute``. A node started without ``python -m topos.compute.gateway``
  has the modules and the route table it has without this package.
* No model residency. The gateway sends the provider nothing by itself, adds
  nothing to a request, and refuses the requests that only load, warm, unload or
  pin a model.
* One overload vocabulary. Its limits, refusal codes and diagnostic keys are the
  ones the Engine HTTP worker budget and ``topos/engine/overload.py`` already use.
"""
import ast
import asyncio
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import httpx
import pytest

from topos.compute import gateway
from topos.compute.gateway import create_app, residency_only
from topos.compute.journal import OwnershipJournal
from topos.compute.provider import ProviderAuthority, Rejected
from topos.engine.admission import AdmissionRejected, EngineAdmission
from topos.engine.overload import unavailable_from_detail

pytestmark = pytest.mark.public
ROOT = Path(__file__).resolve().parents[2]
TOKEN = "fixture-gateway-token-000000000000000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DONE = b'{"response":"Synthetic result","done":true}\n'
MESSAGES = [{"role": "user", "content": "Synthetic input"}]
GATEWAY_MODULES = ("topos.compute.gateway", "topos.compute.journal", "topos.compute.provider")


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class Held(httpx.AsyncByteStream):
    """A provider stream that sends one frame and then waits to be released."""

    def __init__(self, release):
        self.release = release

    async def __aiter__(self):
        yield b'{"response":"First","done":false}\n'
        await self.release.wait()
        yield DONE

    async def aclose(self):
        pass


# --- opt-in: the node does not know the gateway exists --------------------------


def gateway_references(path: Path, source: str) -> list[str]:
    """Every way ``source`` (a module at ``path`` under the repo) reaches the gateway package."""
    package = list(path.relative_to(ROOT).with_suffix("").parts[:-1])
    hits = []

    def check(name, lineno):
        if name == "topos.compute" or name.startswith("topos.compute."):
            hits.append(f"line {lineno}: imports {name}")

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                check(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - (node.level - 1)] if node.level else []
            module = ".".join(base + ([node.module] if node.module else []))
            check(module, node.lineno)
            for alias in node.names:
                check(f"{module}.{alias.name}", node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # importlib.import_module("topos.compute.gateway"), a uvicorn app string, a subprocess argument.
            if node.value == "topos.compute" or node.value.startswith(GATEWAY_MODULES):
                hits.append(f"line {node.lineno}: names {node.value}")
    if "provider_gateway" in source.lower():
        hits.append("reads or names the gateway's own configuration")
    return hits


@pytest.mark.parametrize("where, source", [
    ("topos/app.py", "import topos.compute.gateway\n"),
    ("topos/app.py", "from topos.compute import gateway\n"),
    ("topos/app.py", "from topos import compute\n"),
    ("topos/app.py", "def start():\n    from .compute.gateway import create_app\n"),
    ("topos/api/engine_run.py", "from ..compute import provider\n"),
    ("topos/api/engine_run.py", "from .. import compute\n"),
    ("topos/cli/commands.py", "import importlib\nimportlib.import_module('topos.compute.gateway')\n"),
    ("topos/cli/commands.py", "ARGS = ['-m', 'topos.compute.gateway']\n"),
    ("topos/config/settings.py", "import os\nTOKEN = os.environ.get('TOPOS_PROVIDER_GATEWAY_TOKEN')\n"),
    ("topos/config/settings.py", "topos_provider_gateway_enabled: bool = True\n"),
])
def test_the_guard_sees_each_way_of_reaching_the_gateway(where, source):
    assert gateway_references(ROOT / where, source)


@pytest.mark.parametrize("where, source", [
    ("topos/api/engine_run.py", "measure('counter', 'topos.compute.rejections', 1)\n"),
    ("topos/features/complexity/engine.py", "from .topics import compute_topic_quality\n"),
    ("topos/app.py", "from .engine.admission import EngineAdmission\n"),
])
def test_the_guard_passes_what_only_looks_like_it(where, source):
    assert gateway_references(ROOT / where, source) == []


def test_no_node_module_imports_starts_or_configures_the_gateway():
    """With nothing reaching the package, node startup and every node request path run main's code."""
    offenders = []
    for package in ("topos", "shared"):
        for path in sorted((ROOT / package).rglob("*.py")):
            if (ROOT / "topos" / "compute") in path.parents:
                continue
            for hit in gateway_references(path, path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(f"{path.relative_to(ROOT)}: {hit}")
    assert offenders == [], "the provider gateway must stay a separate, explicitly launched program:\n" + "\n".join(offenders)


def test_a_fresh_node_process_loads_no_gateway_module_and_serves_no_gateway_route(tmp_path):
    """The runtime half of the guard above: import the node's app the way a start does and look."""
    code = (
        "import json, sys\n"
        "import topos.app as node\n"
        "loaded = sorted(m for m in sys.modules if m == 'topos.compute' or m.startswith('topos.compute.'))\n"
        "paths = sorted({getattr(route, 'path', '') for route in node.app.routes})\n"
        "print('RESULT ' + json.dumps({'loaded': loaded, 'paths': paths}))\n"
    )
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop("TOPOS_PROVIDER_GATEWAY_TOKEN", None)
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, text=True,
                            capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("RESULT "))
    seen = json.loads(line.removeprefix("RESULT "))
    assert seen["loaded"] == []
    # The node's own bounded Engine door is there, so this really is the node's route table...
    assert {"/v1/engine/run", "/v1/engine/admission"} <= set(seen["paths"])
    # ...and none of the gateway's three doors is.
    assert not {"/admission", "/api/{kind}", "/api/chat", "/api/generate"} & set(seen["paths"])


def test_the_package_installs_no_command_for_the_gateway():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert not [name for name, target in scripts.items() if "compute" in target or "gateway" in name]


def test_the_launcher_serves_only_when_told_where_and_given_a_secret(tmp_path, monkeypatch):
    import uvicorn

    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.delenv("TOPOS_PROVIDER_GATEWAY_TOKEN", raising=False)
    state = tmp_path / "state"
    explicit = ["gateway", "--upstream", "http://127.0.0.1:11436", "--state-dir", str(state)]

    monkeypatch.setattr(sys, "argv", ["gateway"])
    with pytest.raises(SystemExit):
        gateway.main()  # no provider and no state directory named: there are no defaults for either
    monkeypatch.setattr(sys, "argv", explicit)
    with pytest.raises(ValueError):
        gateway.main()  # no secret in the environment
    monkeypatch.setenv("TOPOS_PROVIDER_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setattr(sys, "argv", explicit + ["--port", "11436"])
    with pytest.raises(SystemExit):
        gateway.main()  # would listen on the provider's own port
    assert served == [] and not state.exists()

    monkeypatch.setattr(sys, "argv", explicit)
    gateway.main()
    assert served == [{"host": "127.0.0.1", "port": 11435, "workers": 1, "access_log": False}]
    assert not state.exists()  # nothing is written until the server actually starts


# --- no model residency ---------------------------------------------------------


@pytest.mark.asyncio
async def test_start_diagnostics_and_stop_send_the_provider_nothing(tmp_path):
    calls = []

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=DONE)

    app = create_app(upstream="http://127.0.0.1:11434", state_dir=tmp_path / "gateway", token=TOKEN,
                     transport=httpx.MockTransport(provider), shutdown_grace=0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway", headers=AUTH) as client:
            for _ in range(3):
                state = (await client.get("/admission")).json()
            assert state["active"] == state["waiting"] == 0
            assert state["totals"]["offered"] == state["totals"]["dispatched"] == 0
    # No probe, no model list, no warm-up, no keep-alive ping: before, during or after.
    assert calls == []


RESIDENCY_ONLY = [
    ("generate", {"model": "fixture"}),                                           # load
    ("generate", {"model": "fixture", "prompt": ""}),                             # load
    ("generate", {"model": "fixture", "prompt": " \n"}),                          # warm-up
    ("generate", {"model": "fixture", "prompt": None}),
    ("generate", {"model": "fixture", "keep_alive": 0}),                          # unload
    ("generate", {"model": "fixture", "prompt": " ", "keep_alive": "0"}),         # unload
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": -1}),      # pin
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": "-1"}),    # pin
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": " -1m"}),  # pin
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": -0.5}),    # pin
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": True}),
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": ["5m"]}),
    ("chat", {"model": "fixture"}),                                               # load
    ("chat", {"model": "fixture", "messages": []}),                               # load
    ("chat", {"model": "fixture", "messages": [], "keep_alive": 0}),              # unload
    ("chat", {"model": "fixture", "prompt": "Synthetic input"}),                  # chat reads messages, not prompt
    ("chat", {"model": "fixture", "messages": MESSAGES, "keep_alive": "-1h"}),    # pin
]


@pytest.mark.parametrize("kind, body", RESIDENCY_ONLY)
def test_what_counts_as_a_residency_request(kind, body):
    assert residency_only(kind, body)


@pytest.mark.parametrize("kind, body", [
    ("generate", {"model": "fixture", "prompt": "Synthetic input"}),
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": None}),
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": 0}),   # answer, then unload
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": "5m"}),
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": 300}),
    ("generate", {"model": "fixture", "prompt": "Synthetic input", "keep_alive": 10 ** 400}),
    ("chat", {"model": "fixture", "messages": MESSAGES}),
    ("chat", {"model": "fixture", "messages": MESSAGES, "keep_alive": "30m"}),
])
def test_generation_with_a_finite_keep_alive_is_not_one(kind, body):
    assert not residency_only(kind, body)


@pytest.mark.asyncio
async def test_load_warm_unload_and_pin_requests_never_reach_admission_or_the_provider(tmp_path):
    calls = []

    def provider(request):
        calls.append(request.content)
        return httpx.Response(200, content=DONE)

    app = create_app(upstream="http://127.0.0.1:11434", state_dir=tmp_path / "gateway", token=TOKEN,
                     transport=httpx.MockTransport(provider), shutdown_grace=0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway", headers=AUTH) as client:
            for kind, body in RESIDENCY_ONLY:
                for stream in (False, True):
                    response = await client.post(f"/api/{kind}", json={**body, "stream": stream})
                    assert response.status_code == 400, (kind, body)
                    assert response.json()["error_type"] == "model_residency_not_proxied"
                    assert "X-Topos-Operation-Id" not in response.headers
            totals = app.state.provider_authority.snapshot()["totals"]
            assert calls == [] and totals["offered"] == totals["admitted"] == totals["dispatched"] == 0

            # A real request is forwarded byte for byte: the gateway adds no keep_alive, option or model of its own.
            sent = json.dumps({"model": "fixture", "prompt": "Synthetic input", "stream": False, "keep_alive": 0}).encode()
            response = await client.post("/api/generate", content=sent)
            assert response.status_code == 200 and response.json()["done"] is True
            assert calls == [sent]
            await until(lambda: not app.state.provider_authority.running)
    assert calls == [sent]


# --- one overload vocabulary ----------------------------------------------------


@pytest.mark.parametrize("reason", ["queue_full", "queue_expired", "stopping"])
def test_a_reason_the_engine_budget_also_has_carries_the_same_outcome(reason):
    ours, engine = Rejected(reason), AdmissionRejected(reason)
    assert (ours.code, ours.status_code, ours.detail(), ours.headers()) == (
        engine.code, engine.status_code, engine.detail(), engine.headers())
    assert ours.headers() == {}  # no retry time is invented on either side


def test_a_reason_nobody_defined_is_refused():
    with pytest.raises(ValueError):
        Rejected("PRIVATE_PROVIDER_DIAGNOSTIC")
    for reason in Rejected.REASONS - {"queue_full", "queue_expired"}:
        assert Rejected(reason).code == "compute_unavailable"


def test_default_limits_are_the_engine_http_budgets():
    from topos.config.settings import Settings

    app = inspect.signature(create_app).parameters
    authority = inspect.signature(ProviderAuthority.__init__).parameters
    engine = inspect.signature(EngineAdmission.__init__).parameters
    fields = Settings.model_fields
    for ours, theirs, setting in (("active", "active", "topos_engine_http_active"),
                                  ("waiting", "waiting", "topos_engine_http_waiting"),
                                  ("queue_seconds", "queue_timeout", "topos_engine_http_queue_timeout_sec")):
        assert app[ours].default == authority[ours].default == engine[theirs].default == fields[setting].default


@pytest.mark.parametrize("limits", [
    {"active": 0}, {"active": 33}, {"active": True}, {"waiting": -1}, {"waiting": 129},
    {"queue": 0}, {"queue": 301},
])
def test_limits_outside_the_engine_budgets_range_are_refused_by_both(limits):
    with pytest.raises(ValueError):
        EngineAdmission(active=limits.get("active", 1), waiting=limits.get("waiting", 4),
                        queue_timeout=limits.get("queue", 30.0))
    with pytest.raises(ValueError):
        ProviderAuthority(None, None, active=limits.get("active", 1), waiting=limits.get("waiting", 4),
                          queue_seconds=limits.get("queue", 30.0))


@pytest.mark.asyncio
async def test_a_full_gateway_answers_in_the_nodes_overload_contract(tmp_path):
    release, calls = asyncio.Event(), []

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, stream=Held(release))

    app = create_app(upstream="http://127.0.0.1:11434", state_dir=tmp_path / "gateway", token=TOKEN,
                     waiting=0, transport=httpx.MockTransport(provider), shutdown_grace=0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway", headers=AUTH) as client:
            held = asyncio.create_task(client.post("/api/generate", json={"model": "fixture", "prompt": "Synthetic input"}))
            await until(lambda: len(calls) == 1)
            refused = await client.post("/api/chat", json={"model": "fixture", "messages": MESSAGES, "stream": False})
            assert refused.status_code == 503 and "retry-after" not in refused.headers
            body = refused.json()
            # What a node client does with a 503 from a compute host.
            outcome = unavailable_from_detail(refused.status_code, body["detail"])
            assert outcome is not None and outcome.code == "compute_busy" == body["error_type"]
            assert body["admission_reason"] == "queue_full" and len(calls) == 1
            release.set()
            assert (await held).status_code == 200


@pytest.mark.asyncio
async def test_a_stopping_gateway_says_stopping_and_its_diagnostics_read_like_the_engine_budgets(tmp_path):
    release = asyncio.Event()
    journal = OwnershipJournal(tmp_path / "authority", "fixture-resource")
    engine = EngineAdmission()
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:11434",
                                     transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Held(release)))) as client:
            gate = ProviderAuthority(journal, client)
            active = gate.submit("/api/generate", b"{}", True)
            await active.headers
            waiting = gate.submit("/api/chat", b"{}", False)

            ours, theirs = gate.snapshot(), engine.snapshot()
            assert set(theirs) <= set(ours)
            assert {key: type(ours[key]) for key in theirs} == {key: type(value) for key, value in theirs.items()}
            assert ours["global_gpu_bound"] is ours["backend_drain_known"] is False
            assert (ours["active_limit"], ours["waiting_limit"], ours["queue_timeout_seconds"]) == (
                theirs["active_limit"], theirs["waiting_limit"], theirs["queue_timeout_seconds"])

            await gate.close(0)
            with pytest.raises(Rejected) as queued:
                await waiting.headers
            with pytest.raises(Rejected) as new:
                gate.submit("/api/chat", b"{}", False)
            assert queued.value.reason == new.value.reason == "stopping"
            assert queued.value.code == AdmissionRejected("stopping").code
    finally:
        engine.close(0)
