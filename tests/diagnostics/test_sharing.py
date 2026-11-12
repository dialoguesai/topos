"""Voluntary diagnostics sharing: the report, the owner's preview, the consent clock, and the fact that this build
cannot send (docs/DIAGNOSTICS_SHARING.md; disclosure rule DR-73).

protects:

* **Nothing leaves the node in this build.** The node's own instance has no sender and no recipient policy, the
  package imports no network library and reads no environment variable or setting, and nothing else under ``topos/``
  builds a ``Sharing`` or hands one a sender. Adding a recipient therefore fails here first, on purpose: whoever
  does it has to change these pins, and the rule in DISCLOSURE_RULES.md, in the same change.
* **What a report can hold.** A fixed set of keys, numbers within bounds, and words from closed lists. Whatever
  else is offered (text, paths, exception words, headers, nested values) is dropped, not copied.
* **Off unless the owner turns it on.** Never on by an OpenTelemetry setting, never kept across instances, off again
  at the hour, at a failed upload, and when the owner stops it; an upload in flight is cancelled, never retried.
* **The owner sees the bytes first.** A one-off send carries exactly the bytes of the preview the owner named by
  hash, once.
* **Who reaches the controls.** The owner's app on the owner socket; not TCP whatever it carries, not an enrolled
  coding agent even with the door split off; sending and switching on also need the owner's confirmation.

Every sender here is a function inside the test. No collector, account, model or database is used.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import topos
import topos.diagnostics.sharing as module
from topos.api.diagnostics import router
from topos.diagnostics.sharing import RecipientPolicy, Sharing, SharingError, report_bytes
from topos.uds import UDSChannelApp

POLICY = RecipientPolicy('test-v1', '30 days; owner can delete earlier',
                         'Approved verified readers only', 'Account and node linked')

ROOT = Path(topos.__file__).resolve().parent
PACKAGE = ROOT / 'diagnostics'
ROUTES = ROOT / 'api' / 'diagnostics.py'

#: Every key a report can hold. A new key is a new disclosure: it needs the owner's word, a new row or an edit in
#: DISCLOSURE_RULES.md (DR-73), and `sharing.DESCRIPTION` (what the owner is told is shared) changed to name it.
REPORT_KEYS = {
    'measurements': {'http_requests', 'http_server_errors', 'http_duration_ms'},
    'system': {'os_family', 'architecture', 'cpu_count'},
}
OS_WORDS = {'Darwin', 'Linux', 'Windows', 'other'}
ARCH_WORDS = {'arm64', 'aarch64', 'x86_64', 'AMD64', 'other'}

#: What the package may import: the standard library's clock, hashing, JSON and platform modules. No socket, HTTP
#: client, subprocess, file or database module, and nothing of the node (no settings, no storage, no telemetry).
PACKAGE_IMPORTS = {'__future__', 'asyncio', 'collections', 'dataclasses', 'hashlib', 'json', 'math', 'os',
                   'platform', 'threading', 'time', 'typing'}
ROUTE_IMPORTS = {'fastapi', 'pydantic', '..uds', '..auth', '..principal', '..diagnostics.sharing'}


@pytest.fixture(autouse=True)
def _the_nodes_instance_is_left_clean():
    yield
    module.sharing._stop()  # drops a preview a route test left on the node's own instance


def _imports(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add('.' * node.level + (node.module or '') if node.level else (node.module or '').split('.')[0])
    return found


# ----------------------------------------------------------------------------- this build cannot send


def test_the_nodes_own_instance_has_no_sender_and_no_policy():
    assert module.sharing._sender is None
    assert module.sharing.policy is None
    assert module.sharing.available is False
    status = module.sharing.status()
    assert status['available'] is False and status['sharing'] is False and status['policy'] is None
    assert status['remaining_seconds'] == 0


def test_the_package_imports_no_network_file_or_node_module():
    files = sorted(PACKAGE.glob('*.py'))
    assert [f.name for f in files] == ['__init__.py', 'sharing.py'], 'a new module: list what it imports here'
    for file in files:
        assert _imports(file) <= PACKAGE_IMPORTS, (file.name, sorted(_imports(file) - PACKAGE_IMPORTS))
    assert _imports(ROUTES) <= ROUTE_IMPORTS, sorted(_imports(ROUTES) - ROUTE_IMPORTS)


def test_no_environment_variable_setting_or_file_can_switch_it_on():
    """By the names the code uses (comments and docstrings may say the words): no read of the environment, of the
    node's settings or its config table, and no file opened."""
    for file in list(PACKAGE.glob('*.py')) + [ROUTES]:
        tree = ast.parse(file.read_text(encoding='utf-8'))
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not used & {'environ', 'getenv', 'settings', 'open', 'engine_config', 'get_db_connection'}, file.name


def test_nothing_in_the_node_builds_a_sharing_or_hands_one_a_sender():
    """The only ``Sharing(...)`` in the node is the bare one in the module, and only the routes and the app's
    middleware line import the module at all."""
    tree = ast.parse((PACKAGE / 'sharing.py').read_text(encoding='utf-8'))
    built = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and getattr(node.func, 'id', None) in ('Sharing', 'RecipientPolicy')]
    assert len(built) == 1 and built[0].func.id == 'Sharing' and not built[0].args and not built[0].keywords

    mention = re.compile(r'diagnostics\.sharing|from \.+diagnostics import')
    reach = re.compile(r'\bSharing\(|RecipientPolicy|_sender|sharing\.policy')
    users = {}
    for file in ROOT.rglob('*.py'):
        if PACKAGE in file.parents:
            continue
        text = file.read_text(encoding='utf-8', errors='replace')
        if mention.search(text):
            users[str(file.relative_to(ROOT))] = bool(reach.search(text))
    assert users == {'api/diagnostics.py': False, 'app.py': False}, users


@pytest.mark.asyncio
async def test_without_a_recipient_both_send_doors_refuse_and_the_preview_still_works(monkeypatch):
    monkeypatch.setenv('TOPOS_OTEL_ENABLED', 'true')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4318')
    sharing = Sharing()
    assert not sharing.status()['sharing']
    assert not sharing.available
    with pytest.raises(SharingError, match='collection_unavailable'):
        await sharing.start_hour('test-v1')
    preview = sharing.preview()
    assert preview['body'] and preview['policy'] is None
    with pytest.raises(SharingError, match='collection_unavailable'):
        await sharing.send_preview(preview['sha256'], 'test-v1')
    assert not sharing.status()['sharing']


@pytest.mark.asyncio
@pytest.mark.parametrize('half', ['sender', 'policy'])
async def test_a_sender_without_a_policy_or_a_policy_without_a_sender_is_still_unavailable(half):
    async def sender(body):
        raise AssertionError('must not send')
    sharing = Sharing(sender=sender) if half == 'sender' else Sharing(policy=POLICY)
    assert sharing.available is False
    with pytest.raises(SharingError, match='collection_unavailable'):
        await sharing.start_hour(POLICY.revision)
    preview = sharing.preview()
    with pytest.raises(SharingError, match='collection_unavailable'):
        await sharing.send_preview(preview['sha256'], POLICY.revision)


def test_a_policy_must_say_all_four_things():
    for blank in range(4):
        words = ['test-v1', 'thirty days', 'named readers', 'linked to the account']
        words[blank] = '  '
        with pytest.raises(ValueError):
            RecipientPolicy(*words)


# ----------------------------------------------------------------------------- what a report can hold


def test_schema_drops_content_and_nonfinite_values():
    raw = dict(http_requests=3, http_server_errors=float('nan'), http_duration_ms=True,
               os_family='Linux', architecture='secret/path', cpu_count=8,
               messages='SECRET', prompts='SECRET', exception='SECRET', stack='SECRET',
               url='SECRET', credentials='SECRET', baggage={'secret': 'SECRET'},
               hostname='SECRET', attributes={'secret': 'SECRET'})
    body = report_bytes(raw)
    assert b'SECRET' not in body
    assert json.loads(body) == {'schema_version': 1, 'measurements': {'http_requests': 3},
                              'system': {'os_family': 'Linux', 'architecture': 'other', 'cpu_count': 8}}


@pytest.mark.parametrize('hostile', [
    '/Users/someone/Library/file.db', 'Bearer abc123', 'ValueError: text of a message', '', 'Linux\n', 'linux',
    b'Linux', ['Linux'], {'Linux': 1}, None, float('inf'), float('-inf'), float('nan'), -1, 10**13, True, False, 1.5,
])
def test_a_hostile_value_in_any_field_is_dropped_never_copied(hostile):
    raw = {key: hostile for group in REPORT_KEYS.values() for key in group}
    report = json.loads(report_bytes(raw))
    assert set(report) == {'schema_version', 'measurements', 'system'} and report['schema_version'] == 1
    assert set(report['measurements']) <= REPORT_KEYS['measurements']
    assert set(report['system']) <= REPORT_KEYS['system']
    for value in report['measurements'].values():
        assert type(value) in (int, float) and value == hostile and 0 <= value
    assert report['system']['os_family'] == 'other' and report['system']['architecture'] == 'other'
    assert 'cpu_count' not in report['system']  # none of these is a whole number from 1 to 4096
    assert all(type(v) in (int, float, str) for group in ('measurements', 'system') for v in report[group].values())


def test_a_real_report_has_exactly_the_named_keys_and_only_closed_words():
    sharing = Sharing()
    sharing.record_http(0.25, False)
    sharing.record_http(0.5, True)
    sharing.record_http(float('nan'), False)  # counted as a request; a time that is not a number adds nothing
    body = sharing._snapshot()
    report = json.loads(body)
    assert set(report) == {'schema_version', 'measurements', 'system'}
    assert report['measurements'] == {'http_requests': 3, 'http_server_errors': 1, 'http_duration_ms': 750.0}
    assert set(report['system']) <= REPORT_KEYS['system']
    assert report['system']['os_family'] in OS_WORDS and report['system']['architecture'] in ARCH_WORDS
    assert type(report['system'].get('cpu_count', 1)) is int
    assert b'/' not in body and b'\\' not in body  # no path can be in it: every word is from a closed list
    # The same bytes for the same numbers: sorted keys, no whitespace, so a hash names one report.
    assert body == json.dumps(report, sort_keys=True, separators=(',', ':')).encode()


def test_totals_stop_at_their_ceilings():
    sharing = Sharing()
    for key, ceiling in (('http_requests', 10**9), ('http_server_errors', 10**9), ('http_duration_ms', 10**12)):
        sharing._counts[key] = ceiling
    sharing.record_http(5.0, True)
    report = json.loads(sharing._snapshot())
    assert report['measurements'] == {'http_requests': 10**9, 'http_server_errors': 10**9, 'http_duration_ms': 10**12}


@pytest.mark.asyncio
async def test_middleware_never_records_path_headers_or_exception(monkeypatch):
    sharing = Sharing()
    monkeypatch.setattr(module, 'sharing', sharing)
    async def app(scope, receive, send):
        raise ValueError('SECRET_EXCEPTION')
    wrapped = module.DiagnosticsMeasurements(app)
    with pytest.raises(ValueError):
        await wrapped({'type': 'http', 'path': '/SECRET_PATH',
                       'headers': [(b'authorization', b'SECRET_TOKEN')]}, None, None)
    preview = sharing.preview()
    assert 'SECRET' not in preview['body']
    assert json.loads(preview['body'])['measurements']['http_server_errors'] == 1
    await sharing.revoke()


@pytest.mark.asyncio
async def test_middleware_counts_a_5xx_as_an_error_a_4xx_as_a_request_and_nothing_else(monkeypatch):
    sharing = Sharing()
    monkeypatch.setattr(module, 'sharing', sharing)
    reached = []

    def answering(status):
        async def app(scope, receive, send):
            reached.append(scope['type'])
            if scope['type'] == 'http':
                await send({'type': 'http.response.start', 'status': status, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'SECRET_BODY'})
        return app

    async def send(message):
        pass

    for status in (200, 404, 503):
        await module.DiagnosticsMeasurements(answering(status))({'type': 'http', 'path': '/SECRET_PATH'}, None, send)
    for kind in ('websocket', 'lifespan'):
        await module.DiagnosticsMeasurements(answering(200))({'type': kind}, None, send)
    assert reached == ['http', 'http', 'http', 'websocket', 'lifespan']
    body = sharing._snapshot()
    measurements = json.loads(body)['measurements']
    assert measurements['http_requests'] == 3 and measurements['http_server_errors'] == 1
    assert b'SECRET' not in body


# ----------------------------------------------------------------------------- the owner sees the bytes first


@pytest.mark.asyncio
async def test_exact_preview_single_use_and_policy_binding():
    sent = []
    async def sender(body):
        sent.append(body)
    sharing = Sharing(sender=sender, policy=POLICY)
    sharing.record_http(.25, False)
    preview = sharing.preview()
    sharing.record_http(.5, True)
    with pytest.raises(SharingError, match='policy_changed'):
        await sharing.send_preview(preview['sha256'], 'old')
    await sharing.send_preview(preview['sha256'], POLICY.revision)
    assert sent == [preview['body'].encode()]
    assert hashlib.sha256(sent[0]).hexdigest() == preview['sha256']
    with pytest.raises(SharingError, match='preview_missing'):
        await sharing.send_preview(preview['sha256'], POLICY.revision)
    assert not sharing.status()['sharing']


@pytest.mark.asyncio
async def test_a_hash_the_owner_was_not_shown_sends_nothing():
    async def sender(body):
        raise AssertionError('must not send')
    sharing = Sharing(sender=sender, policy=POLICY)
    with pytest.raises(SharingError, match='preview_missing'):
        await sharing.send_preview('0' * 64, POLICY.revision)  # no preview at all
    first = sharing.preview()
    sharing.record_http(1.0, False)
    second = sharing.preview()  # a newer preview replaces the older one
    assert first['sha256'] != second['sha256']
    with pytest.raises(SharingError, match='preview_missing'):
        await sharing.send_preview(first['sha256'], POLICY.revision)
    await sharing.revoke()


@pytest.mark.asyncio
async def test_preview_expires():
    now = [0.0]
    async def sender(body):
        raise AssertionError('must not send')
    sharing = Sharing(sender=sender, policy=POLICY, clock=lambda: now[0])
    preview = sharing.preview()
    now[0] = 300
    with pytest.raises(SharingError, match='preview_missing'):
        await sharing.send_preview(preview['sha256'], POLICY.revision)


# ----------------------------------------------------------------------------- the consent clock


@pytest.mark.asyncio
async def test_expiry_discards_preview_and_stops_session():
    now = [10.0]
    sent = []
    async def sender(body):
        sent.append(body)
    sharing = Sharing(sender=sender, policy=POLICY, clock=lambda: now[0], interval=.001)
    await sharing.start_hour(POLICY.revision)
    preview = sharing.preview()
    now[0] += 3601
    assert not sharing.status()['sharing']
    await asyncio.sleep(.01)
    assert sent == []
    with pytest.raises(SharingError, match='preview_missing'):
        await sharing.send_preview(preview['sha256'], POLICY.revision)


@pytest.mark.asyncio
@pytest.mark.parametrize('timed', [True, False])
async def test_revoke_cancels_inflight_and_no_retries(timed):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    sent = []
    async def sender(body):
        entered.set()
        try:
            await asyncio.Event().wait()
            sent.append(body)
        finally:
            cancelled.set()
    sharing = Sharing(sender=sender, policy=POLICY, interval=.001)
    if timed:
        await sharing.start_hour(POLICY.revision)
        task = None
    else:
        preview = sharing.preview()
        task = asyncio.create_task(sharing.send_preview(preview['sha256'], POLICY.revision))
    await asyncio.wait_for(entered.wait(), 1)
    await sharing.revoke()
    assert cancelled.is_set()
    if task:
        with pytest.raises(SharingError, match='sharing_stopped'):
            await task
    await asyncio.sleep(.01)
    assert not sharing.status()['sharing']
    assert sent == []
    assert sharing._preview is None


@pytest.mark.asyncio
async def test_upload_failure_stops_timed_consent():
    calls = []
    async def sender(body):
        calls.append(body)
        raise ValueError('SECRET EXCEPTION URL')
    sharing = Sharing(sender=sender, policy=POLICY, interval=.001)
    await sharing.start_hour(POLICY.revision)
    await asyncio.sleep(.02)
    assert len(calls) == 1
    assert not sharing.status()['sharing']


@pytest.mark.asyncio
async def test_a_failed_one_off_send_says_one_fixed_word_and_is_not_retried():
    calls = []
    async def sender(body):
        calls.append(body)
        raise ValueError('SECRET EXCEPTION URL')
    sharing = Sharing(sender=sender, policy=POLICY)
    preview = sharing.preview()
    with pytest.raises(SharingError) as refused:
        await sharing.send_preview(preview['sha256'], POLICY.revision)
    assert str(refused.value) == 'upload_failed'  # never the transport's own words
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    with pytest.raises(SharingError, match='preview_missing'):
        await sharing.send_preview(preview['sha256'], POLICY.revision)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expiry_cancels_inflight_upload():
    now = [1.0]
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def sender(body):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    sharing = Sharing(sender=sender, policy=POLICY, clock=lambda: now[0], interval=.001)
    await sharing.start_hour(POLICY.revision)
    await asyncio.wait_for(entered.wait(), 1)
    now[0] += 3600
    assert not sharing.status()['sharing']
    await asyncio.wait_for(cancelled.wait(), 1)
    assert sharing._preview is None


@pytest.mark.asyncio
async def test_concurrent_starts_do_not_leave_old_sessions():
    calls = []
    async def sender(body):
        calls.append(body)
    sharing = Sharing(sender=sender, policy=POLICY, interval=.001)
    await asyncio.gather(*(sharing.start_hour(POLICY.revision) for _ in range(5)))
    await asyncio.sleep(.01)
    await sharing.revoke()
    count = len(calls)
    await asyncio.sleep(.01)
    assert len(calls) == count
    assert not sharing.status()['sharing']


@pytest.mark.asyncio
async def test_expiry_timer_stops_inflight_without_status_poll(monkeypatch):
    loop = asyncio.get_running_loop()
    original = loop.call_later
    monkeypatch.setattr(loop, 'call_later',
                        lambda delay, callback, *args, **kwargs: original(
                            .03 if delay == 3600 else delay, callback, *args, **kwargs))
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def sender(body):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    sharing = Sharing(sender=sender, policy=POLICY, interval=.001)
    await sharing.start_hour(POLICY.revision)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert sharing._deadline == 0
    assert sharing._session is None


@pytest.mark.asyncio
async def test_node_instances_have_independent_consent():
    async def sender(body):
        pass
    first, second = Sharing(sender=sender, policy=POLICY), Sharing(sender=sender, policy=POLICY)
    await first.start_hour(POLICY.revision)
    assert first.status()['sharing']
    assert not second.status()['sharing']
    await second.revoke()
    assert first.status()['sharing']
    await first.revoke()


# ----------------------------------------------------------------------------- who reaches the controls


def test_application_mounts_diagnostics_routes():
    from topos.app import app
    paths = {route.path for route in app.routes}
    assert '/diagnostics/dialogues/preview' in paths
    assert '/diagnostics/dialogues/share-one-hour' in paths


def test_the_census_gates_sending_and_switching_on_and_leaves_looking_and_stopping_open():
    from topos import owner_door_routes as census
    assert census.lookup('POST', '/diagnostics/dialogues/send').verdict == census.DANGEROUS
    assert census.lookup('POST', '/diagnostics/dialogues/share-one-hour').verdict == census.DANGEROUS
    assert census.lookup('POST', '/diagnostics/dialogues/preview').verdict == census.OPEN
    assert census.lookup('DELETE', '/diagnostics/dialogues').verdict == census.OPEN
    for method, path in [('GET', '/diagnostics/dialogues'), ('POST', '/diagnostics/dialogues/preview'),
                         ('DELETE', '/diagnostics/dialogues'), ('POST', '/diagnostics/dialogues/send')]:
        assert not census.owner_agent_route_allowed(method, path)
        assert not census.app_agent_route_allowed(method, path)


@pytest.mark.asyncio
async def test_every_route_rejects_tcp_even_with_owner_headers():
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
        for method, path in [('GET', ''), ('DELETE', ''), ('POST', '/preview'),
                             ('POST', '/send'), ('POST', '/share-one-hour')]:
            response = await client.request(method, '/diagnostics/dialogues' + path,
                                            headers={'Authorization': 'Bearer owner', 'X-Transport': 'uds'})
            assert response.status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UDSChannelApp(app)), base_url='http://local') as client:
        assert (await client.get('/diagnostics/dialogues')).json()['sharing'] is False
        assert (await client.post('/diagnostics/dialogues/preview')).status_code == 200
        assert (await client.post('/diagnostics/dialogues/share-one-hour', json={'policy_revision': 'v1'})).status_code == 403


@pytest.mark.asyncio
async def test_send_and_enable_require_owner_confirmation_but_stop_does_not(monkeypatch):
    from topos import uds
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UDSChannelApp(app)), base_url='http://local') as client:
        for path in ('send', 'share-one-hour'):
            response = await client.post('/diagnostics/dialogues/' + path, json={})
            assert response.status_code == 403
            assert response.json()['error'] == 'owner_confirmation_required'
        assert (await client.delete('/diagnostics/dialogues')).status_code == 200
        assert (await client.post('/diagnostics/dialogues/preview')).status_code == 200
        # Synthetic already-confirmed socket peer; no token written/read.
        monkeypatch.setattr(uds, 'socket_peer_from_scope', lambda scope: uds.SocketPeer(pid=None, executable=None, confirmed=True))
        response = await client.post('/diagnostics/dialogues/share-one-hour', json={'policy_revision': 'v1'})
        assert response.status_code == 409
        assert response.json()['detail'] == 'collection_unavailable'


@pytest.mark.asyncio
async def test_a_confirmed_request_must_still_be_well_formed(monkeypatch):
    from topos import uds
    monkeypatch.setattr(uds, 'socket_peer_from_scope', lambda scope: uds.SocketPeer(pid=None, executable=None, confirmed=True))
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UDSChannelApp(app)), base_url='http://local') as client:
        for path, body in [('share-one-hour', {}), ('share-one-hour', {'policy_revision': 'v1', 'consent': True}),
                           ('send', {'policy_revision': 'v1'}), ('send', {'policy_revision': 'v1', 'sha256': 'XYZ'}),
                           ('send', {'policy_revision': 'v1', 'sha256': '0' * 64, 'body': '{}'})]:
            response = await client.post('/diagnostics/dialogues/' + path, json=body)
            assert response.status_code == 422, (path, body)


@pytest.mark.asyncio
async def test_agent_cannot_manage_diagnostics_even_if_owner_door_split_is_off(monkeypatch):
    from topos import uds
    from topos.principal import OWNER_AGENT, Principal
    from topos.api import diagnostics
    principal = Principal(cls=OWNER_AGENT, channel='uds', client_id='synthetic-agent')
    async def resolve(scope):
        return principal, None
    monkeypatch.setattr(uds, 'resolve_socket_principal', resolve)
    monkeypatch.setenv('TOPOS_OWNER_DOOR_SPLIT', '0')
    monkeypatch.setattr(diagnostics, 'resolve_request_principal', lambda *args: principal)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UDSChannelApp(app)), base_url='http://local') as client:
        for method, suffix in [('GET', ''), ('DELETE', ''), ('POST', '/preview'), ('POST', '/send'), ('POST', '/share-one-hour')]:
            assert (await client.request(method, '/diagnostics/dialogues' + suffix)).status_code == 403
        # Even if the outer agent allowlist were widened, the route stays owner-app only.
        monkeypatch.setattr(uds, 'owner_agent_route_allowed', lambda *args: True)
        assert (await client.get('/diagnostics/dialogues')).status_code == 403


@pytest.mark.asyncio
async def test_the_real_app_previews_on_the_socket_and_cannot_send_even_when_confirmed(tmp_path, monkeypatch):
    """The node as it ships, through the socket wrapper: the owner can read the report; a send the owner confirmed
    with a real one-use token still has nowhere to go."""
    from topos import uds
    from topos.app import app
    monkeypatch.setenv('TOPOS_UDS_PATH', str(tmp_path / 'engine.sock'))  # the confirmation file lives beside it
    monkeypatch.setenv('TOPOS_OWNER_DOOR_SPLIT', '1')
    for name in ('TOPOS_OWNER_DOOR_TEAM_IDS', 'TOPOS_UDS_TEAM_IDS', 'TOPOS_DEV_PROXY_ALLOWED'):
        monkeypatch.delenv(name, raising=False)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as tcp:
        refused = await tcp.get('/diagnostics/dialogues', headers={'Authorization': 'Bearer anything'})
        assert refused.status_code == 403 and refused.json()['detail'] == 'owner_socket_required'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=UDSChannelApp(app)), base_url='http://local') as client:
        status = (await client.get('/diagnostics/dialogues')).json()
        assert status['available'] is False and status['sharing'] is False and status['policy'] is None
        assert status['label'] == module.LABEL and status['description'] == module.DESCRIPTION

        preview = (await client.post('/diagnostics/dialogues/preview')).json()
        report = json.loads(preview['body'])
        assert hashlib.sha256(preview['body'].encode()).hexdigest() == preview['sha256']
        assert set(report) == {'schema_version', 'measurements', 'system'}
        assert set(report['measurements']) == REPORT_KEYS['measurements']
        assert report['measurements']['http_requests'] >= 1  # the app's middleware counted the requests above
        assert preview['policy'] is None and preview['expires_in_seconds'] == 300

        for path, body in [('send', {'sha256': preview['sha256'], 'policy_revision': 'any'}),
                           ('share-one-hour', {'policy_revision': 'any'})]:
            unconfirmed = await client.post('/diagnostics/dialogues/' + path, json=body)
            assert unconfirmed.status_code == 403 and unconfirmed.json()['error'] == 'owner_confirmation_required'
            confirmed = await client.post('/diagnostics/dialogues/' + path, json=body,
                                          headers={'X-Topos-Owner-Confirm': uds.write_owner_confirmation()})
            assert confirmed.status_code == 409 and confirmed.json()['detail'] == 'collection_unavailable'

        stopped = await client.delete('/diagnostics/dialogues')
        assert stopped.status_code == 200 and stopped.json()['sharing'] is False
