"""Shared pytest fixtures.

Sets fake RADARR_URL/RADARR_API_KEY *before* importing server.py, since the
module requires both at import time (see server._require_env). Individual
tests then swap server.client's transport to control what "Radarr" returns,
via httpx.MockTransport — no extra mocking library needed, it ships in httpx
(already a runtime dependency).
"""

import os

os.environ.setdefault("RADARR_URL", "http://test-radarr:7878")
os.environ.setdefault("RADARR_API_KEY", "test-api-key")

import httpx  # noqa: E402
import pytest  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def mock_radarr(monkeypatch):
    """Point server.client at a fake Radarr.

    Also stubs server.discovery_client (the unversioned GET /api endpoint)
    to report the configured RADARR_API_VERSION as current, so /ready tests
    that don't care about version drift aren't affected by it. Use
    mock_discovery to override that.

    Usage:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[...])
        mock_radarr(handler)
    """

    def _install(handler):
        fake_client = httpx.Client(
            base_url=f"{server.RADARR_URL}/api/{server.RADARR_API_VERSION}",
            headers={"X-Api-Key": server.RADARR_API_KEY},
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(server, "client", fake_client)

        fake_discovery = httpx.Client(
            base_url=server.RADARR_URL,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"current": server.RADARR_API_VERSION, "deprecated": []})
            ),
        )
        monkeypatch.setattr(server, "discovery_client", fake_discovery)

        return fake_client

    return _install


@pytest.fixture
def mock_discovery(monkeypatch):
    """Override server.discovery_client's response, e.g. to simulate a Radarr
    that no longer serves the configured RADARR_API_VERSION."""

    def _install(handler):
        fake_discovery = httpx.Client(
            base_url=server.RADARR_URL,
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(server, "discovery_client", fake_discovery)
        return fake_discovery

    return _install


@pytest.fixture
def no_auth(monkeypatch):
    """Run with MCP_AUTH_TOKEN unset (the default, open-server mode)."""
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", None)


@pytest.fixture
def with_auth(monkeypatch):
    """Run with a known MCP_AUTH_TOKEN, and return it."""
    token = "test-shared-secret"
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", token)
    return token
