"""Tests for the wired-together ASGI app: /health, /ready, and the bearer-auth
middleware — via server.build_app(), the same function __main__ uses."""

import httpx
from starlette.testclient import TestClient

import server


def test_health_does_not_call_radarr(mock_radarr, no_auth):
    def handler(request):
        raise AssertionError("system_status /health must not call Radarr")

    mock_radarr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_success(mock_radarr, no_auth):
    mock_radarr(lambda req: httpx.Response(200, json={"version": "5.0.9"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body == {
        "status": "ok",
        "reachable": True,
        "authenticated": True,
        "radarr": {"url": server.RADARR_URL, "version": "5.0.9"},
        "apiVersion": {
            "checked": True,
            "configured": server.RADARR_API_VERSION,
            "current": server.RADARR_API_VERSION,
            "deprecated": [],
            "supported": True,
        },
    }


def test_ready_invalid_api_key(mock_radarr, no_auth):
    mock_radarr(lambda req: httpx.Response(401, json={"message": "Unauthorized"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is False
    assert "invalid Radarr API key" in body["error"]


def test_ready_unreachable_host(mock_radarr, no_auth):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    mock_radarr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is False
    assert body["authenticated"] is False


def test_ready_other_radarr_error(mock_radarr, no_auth):
    mock_radarr(lambda req: httpx.Response(500, json={"message": "boom"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert "HTTP 500" in body["error"]


def test_ready_unsupported_api_version(mock_radarr, mock_discovery, no_auth):
    mock_radarr(lambda req: httpx.Response(200, json={"version": "6.0.0"}))
    mock_discovery(lambda req: httpx.Response(200, json={"current": "v4", "deprecated": ["v2"]}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert body["apiVersion"] == {
        "checked": True,
        "configured": server.RADARR_API_VERSION,
        "current": "v4",
        "deprecated": ["v2"],
        "supported": False,
    }
    assert server.RADARR_API_VERSION in body["error"]


def test_ready_deprecated_but_still_supported_api_version(mock_radarr, mock_discovery, no_auth):
    mock_radarr(lambda req: httpx.Response(200, json={"version": "6.0.0"}))
    mock_discovery(
        lambda req: httpx.Response(200, json={"current": "v4", "deprecated": [server.RADARR_API_VERSION]})
    )

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["apiVersion"]["supported"] is True


def test_ready_discovery_endpoint_unavailable_is_non_fatal(mock_radarr, mock_discovery, no_auth):
    mock_radarr(lambda req: httpx.Response(200, json={"version": "3.0.0"}))

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    mock_discovery(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["apiVersion"] == {"checked": False}


def test_no_auth_token_leaves_mcp_open(mock_radarr, no_auth):
    mock_radarr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    # No 401 — auth is off. The exact protocol response doesn't matter here,
    # only that the request wasn't rejected by the auth layer.
    assert response.status_code != 401


def test_auth_token_blocks_mcp_without_header(mock_radarr, with_auth):
    mock_radarr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/mcp", headers={"Accept": "application/json, text/event-stream"})

    assert response.status_code == 401


def test_auth_token_blocks_mcp_with_wrong_token(mock_radarr, with_auth):
    mock_radarr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer wrong-token",
            },
        )

    assert response.status_code == 401


def test_auth_token_allows_mcp_with_correct_token(mock_radarr, with_auth):
    mock_radarr(lambda req: httpx.Response(200, json={}))
    token = with_auth

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )

    # Past auth — the normal MCP protocol response for a bare GET with no
    # prior session (400 missing-session), not a 401.
    assert response.status_code != 401


def test_auth_token_does_not_block_health_or_ready(mock_radarr, with_auth):
    mock_radarr(lambda req: httpx.Response(200, json={"version": "5.0.9"}))

    with TestClient(server.build_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
