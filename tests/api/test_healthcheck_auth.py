"""/healthcheck under ENABLE_HEALTH_AUTH.

protects: the gated healthcheck answers 401 to a missing or wrong bearer and 200
to a right one. It is a regression test for a 500: `require_api_key` grew a
leading `request` parameter with the principal fabric, and this route calls it
directly rather than through FastAPI's dependency injection, so a positional
call bound the credentials to `request` and left `credentials` holding its
`Depends(...)` default — `'Depends' object has no attribute 'scheme'`, raised
inside the handler, surfacing as a 500 where the route means to refuse.

Nothing in the engine suite exercised the gated path (the setting defaults off),
so the break was only visible from the control plane, which imports this module
through the sibling checkout and does turn the setting on.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos.api.health import router


@pytest.fixture()
def client(monkeypatch):
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "enable_health_auth", True, raising=False)
    monkeypatch.setattr(runtime_settings, "topos_key", "right-key", raising=False)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", None, raising=False)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_no_bearer_is_refused_not_a_server_error(client):
    response = client.get("/healthcheck")
    assert response.status_code == 401, response.text


def test_a_wrong_bearer_is_refused(client):
    response = client.get("/healthcheck", headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 401, response.text


def test_the_right_bearer_is_let_through(client):
    response = client.get("/healthcheck", headers={"Authorization": "Bearer right-key"})
    assert response.status_code == 200, response.text
