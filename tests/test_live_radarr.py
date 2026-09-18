"""Live contract tests against a *real* Radarr instance.

These do not run by default — there's no Radarr in CI, and we don't want to
accidentally fire real requests using the fake RADARR_URL/RADARR_API_KEY that
conftest.py sets for the rest of the suite. To run them:

    RUN_LIVE_RADARR_TESTS=1 RADARR_URL=https://radarr.example.com \\
    RADARR_API_KEY=<real key> pytest tests/test_live_radarr.py -v

Point is to catch drift: if a Radarr upgrade renames/removes a field our
tools depend on (id, title, hasFile, tmdbId, version, ...), these fail even
though the mocked unit tests in test_tools.py would still happily pass (they
only assert against fixtures we wrote ourselves).
"""

import os

import httpx
import pytest

import server

RUN_LIVE = os.environ.get("RUN_LIVE_RADARR_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="opt-in only: set RUN_LIVE_RADARR_TESTS=1 with a real RADARR_URL/RADARR_API_KEY",
)


@pytest.fixture(scope="module")
def live_client():
    return httpx.Client(
        base_url=f"{server.RADARR_URL}/api/v3",
        headers={"X-Api-Key": server.RADARR_API_KEY},
        timeout=15,
    )


def test_system_status_shape(live_client):
    """The fields system_status()/`/ready` depend on actually exist."""
    response = live_client.get("/system/status")
    response.raise_for_status()
    data = response.json()

    assert isinstance(data.get("version"), str) and data["version"].count(".") >= 2
    assert "instanceName" in data


def test_movie_shape_matches_what_list_movies_assumes(live_client):
    """Every field list_movies() reads with .get() (safe) or [..] (required) exists."""
    response = live_client.get("/movie")
    response.raise_for_status()
    movies = response.json()

    assert isinstance(movies, list)
    if not movies:
        pytest.skip("library is empty — nothing to validate the shape of")

    sample = movies[0]
    for required_field in ("id", "title"):
        assert required_field in sample, f"Radarr's /movie no longer returns '{required_field}'"

    # These are read with .get() in our code specifically because they're
    # not guaranteed — but if Radarr stops sending them for every record,
    # list_movies() silently degrades, which is worth knowing about here.
    for soft_field in ("year", "monitored", "status", "studio", "hasFile"):
        if soft_field not in sample:
            pytest.skip(f"'{soft_field}' missing from a real record — list_movies() will report it as null")


def test_movie_lookup_shape(live_client):
    """The fields lookup_movie() depends on for a real search term."""
    response = live_client.get("/movie/lookup", params={"term": "Chernobyl"})
    response.raise_for_status()
    results = response.json()

    assert isinstance(results, list) and len(results) > 0
    sample = results[0]
    assert "title" in sample
    assert "tmdbId" in sample


def test_our_tools_run_cleanly_against_real_radarr(monkeypatch, live_client):
    """Run the actual tool functions (not just raw requests) against real Radarr."""
    monkeypatch.setattr(server, "client", live_client)

    status = server.system_status()
    assert "version" in status["status"]

    movies = server.list_movies()
    assert isinstance(movies, list)

    results = server.lookup_movie("Chernobyl")
    assert len(results) > 0
    assert results[0]["title"]
