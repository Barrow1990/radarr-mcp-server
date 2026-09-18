"""Unit tests for each MCP tool's logic, against a mocked Radarr.

`@mcp.tool()` returns the original function unchanged, so these call the
tools directly as plain Python functions — no MCP protocol/session machinery
involved here (that's covered separately in test_http.py).
"""

import httpx
import pytest

import server

SAMPLE_MOVIES = [
    {
        "id": 1,
        "title": "Chernobyl",
        "year": 2019,
        "monitored": True,
        "status": "released",
        "studio": "HBO",
        "hasFile": True,
    },
    {
        "id": 2,
        "title": "The Wire",
        "year": 2002,
        "monitored": True,
        "status": "released",
        "studio": "HBO",
        "hasFile": False,
    },
]


def test_list_movies_returns_shaped_records(mock_radarr):
    mock_radarr(lambda req: httpx.Response(200, json=SAMPLE_MOVIES))

    result = server.list_movies()

    assert len(result) == 2
    assert result[0] == {
        "id": 1,
        "title": "Chernobyl",
        "year": 2019,
        "monitored": True,
        "status": "released",
        "studio": "HBO",
        "hasFile": True,
    }


def test_list_movies_filters_by_title_case_insensitive(mock_radarr):
    mock_radarr(lambda req: httpx.Response(200, json=SAMPLE_MOVIES))

    result = server.list_movies(title="wire")

    assert len(result) == 1
    assert result[0]["title"] == "The Wire"


def test_list_movies_missing_optional_fields_default_to_none(mock_radarr):
    mock_radarr(lambda req: httpx.Response(200, json=[{"id": 1, "title": "No Stats"}]))

    result = server.list_movies()

    assert result[0]["hasFile"] is None
    assert result[0]["studio"] is None


def test_list_movies_propagates_http_errors(mock_radarr):
    mock_radarr(lambda req: httpx.Response(500, json={"message": "boom"}))

    with pytest.raises(httpx.HTTPStatusError):
        server.list_movies()


def test_movie_details_hits_correct_path(mock_radarr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie/42"
        return httpx.Response(200, json={"id": 42, "title": "Chernobyl"})

    mock_radarr(handler)

    result = server.movie_details(42)

    assert result == {"id": 42, "title": "Chernobyl"}


def test_missing_movies_hits_correct_path(mock_radarr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/wanted/missing"
        assert request.url.params["pageSize"] == "200"
        return httpx.Response(200, json={"records": [{"id": 1}]})

    mock_radarr(handler)

    assert server.missing_movies() == [{"id": 1}]


def test_lookup_movie_shapes_and_truncates_overview(mock_radarr):
    long_overview = "x" * 500

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/movie/lookup"
        assert request.url.params["term"] == "chernobyl"
        return httpx.Response(
            200,
            json=[
                {
                    "title": "Chernobyl",
                    "year": 2019,
                    "tmdbId": 360893,
                    "overview": long_overview,
                    "studio": "HBO",
                }
            ],
        )

    mock_radarr(handler)

    result = server.lookup_movie("chernobyl")

    assert result[0]["title"] == "Chernobyl"
    assert result[0]["tmdbId"] == 360893
    assert len(result[0]["overview"]) == 300


def test_lookup_movie_handles_missing_overview(mock_radarr):
    mock_radarr(lambda req: httpx.Response(200, json=[{"title": "X", "overview": None}]))

    result = server.lookup_movie("x")

    assert result[0]["overview"] == ""


def test_search_movie_posts_correct_command(mock_radarr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/command"
        import json

        body = json.loads(request.content)
        assert body == {"name": "MovieSearch", "movieIds": [99]}
        return httpx.Response(201, json={"id": 123, "status": "queued"})

    mock_radarr(handler)

    result = server.search_movie(99)

    assert result == "Search triggered for movie 99"


def test_system_status_combines_three_endpoints(mock_radarr):
    def handler(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/api/v3/system/status": {"version": "5.0.9"},
            "/api/v3/diskspace": [{"freeSpace": 123}],
            "/api/v3/health": [{"type": "warning"}],
        }
        return httpx.Response(200, json=payloads[request.url.path])

    mock_radarr(handler)

    result = server.system_status()

    assert result["status"]["version"] == "5.0.9"
    assert result["diskSpace"] == [{"freeSpace": 123}]
    assert result["health"] == [{"type": "warning"}]
