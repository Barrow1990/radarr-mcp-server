"""
Radarr MCP Server

Exposes a small set of Radarr (movie library manager) operations as MCP
tools, so an MCP-compatible AI assistant (e.g. Claude Code / Claude Desktop)
can browse your library, check for missing movies, look up new movies, and
trigger downloads via Radarr's REST API.

Configuration is via environment variables:
  RADARR_URL          e.g. http://192.168.1.50:7878 (required)
  RADARR_API_KEY      Radarr > Settings > General > API Key (required)
  RADARR_API_VERSION  Radarr REST API version to call, e.g. "v3" (default "v3")
  MCP_HOST            interface to bind to (default 0.0.0.0)
  MCP_PORT            port to listen on (default 8932)
  MCP_AUTH_TOKEN      shared secret required as `Authorization: Bearer <token>`
                      on every request (optional — if unset, the server is open
                      to anyone who can reach it; see README for why that's a
                      real trade-off, not just a default to ignore)

Radarr shares its underlying HTTP framework with Sonarr (both are Servarr
apps built on the same *arr common codebase), so it exposes the same
unauthenticated, unversioned `GET /api` discovery endpoint reporting which
API version is current and which are deprecated, e.g. {"current": "v3",
"deprecated": []}. GET /ready calls it and compares RADARR_API_VERSION
against that response, so a Radarr upgrade that drops the version this
server is calling shows up as a readiness failure instead of every tool
call silently 404ing.

Transport: streamable-http. This runs as a standing network service (bind
0.0.0.0 inside the container; publish the port only on your internal
network/VLAN — never forward it externally) rather than being spawned
per-client over stdio, so any MCP client on the LAN can connect to
http://<host>:<port>/mcp.

Auth here is a single shared bearer token checked by plain middleware, not
the SDK's built-in OAuth support (mcp.server.auth) — that machinery expects
a full OAuth authorization server (issuer/resource metadata, RFC 8414/8707/
9068 discovery), which is unwarranted complexity for a single internal
secret shared by trusted LAN clients.
"""

import hmac
import os
import sys

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


RADARR_URL = _require_env("RADARR_URL").rstrip("/")
RADARR_API_KEY = _require_env("RADARR_API_KEY")
RADARR_API_VERSION = os.environ.get("RADARR_API_VERSION", "v3")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8932"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")

client = httpx.Client(
    base_url=f"{RADARR_URL}/api/{RADARR_API_VERSION}",
    headers={"X-Api-Key": RADARR_API_KEY},
    timeout=30,
)

# Separate client for Radarr's unauthenticated, unversioned /api discovery
# endpoint (not under /api/{version}, and needs no X-Api-Key).
discovery_client = httpx.Client(base_url=RADARR_URL, timeout=5)

mcp = MCPServer("radarr")


@mcp.tool()
def list_movies(title: str | None = None) -> list[dict]:
    """List movies already in the Radarr library, optionally filtered by a title substring."""
    response = client.get("/movie")
    response.raise_for_status()
    movies = response.json()

    if title:
        needle = title.lower()
        movies = [m for m in movies if needle in m["title"].lower()]

    return [
        {
            "id": m["id"],
            "title": m["title"],
            "year": m.get("year"),
            "monitored": m.get("monitored"),
            "status": m.get("status"),
            "studio": m.get("studio"),
            "hasFile": m.get("hasFile"),
        }
        for m in movies
    ]


@mcp.tool()
def movie_details(movie_id: int) -> dict:
    """Get full details for a single movie by its Radarr ID."""
    response = client.get(f"/movie/{movie_id}")
    response.raise_for_status()
    return response.json()


@mcp.tool()
def missing_movies() -> list[dict]:
    """List monitored movies that don't have a file yet (Radarr's wanted/missing list)."""
    response = client.get("/wanted/missing", params={"pageSize": 200})
    response.raise_for_status()
    return response.json()["records"]


@mcp.tool()
def lookup_movie(term: str) -> list[dict]:
    """Search for new movies to potentially add to Radarr, by title. Does not add anything."""
    response = client.get("/movie/lookup", params={"term": term})
    response.raise_for_status()
    return [
        {
            "title": m["title"],
            "year": m.get("year"),
            "tmdbId": m.get("tmdbId"),
            "overview": (m.get("overview") or "")[:300],
            "studio": m.get("studio"),
        }
        for m in response.json()
    ]


@mcp.tool()
def search_movie(movie_id: int) -> str:
    """Trigger Radarr to search for a movie that is already in the library."""
    # Radarr's MovieSearch command takes a movieIds *array* even for a single
    # movie — unlike Sonarr's SeriesSearch, which takes a single seriesId.
    response = client.post("/command", json={"name": "MovieSearch", "movieIds": [movie_id]})
    response.raise_for_status()
    return f"Search triggered for movie {movie_id}"


@mcp.tool()
def system_status() -> dict:
    """Get Radarr system status, disk space, and health checks."""
    status, disk_space, health = (
        client.get("/system/status").json(),
        client.get("/diskspace").json(),
        client.get("/health").json(),
    )
    return {"status": status, "diskSpace": disk_space, "health": health}


# Paths that must stay reachable without MCP_AUTH_TOKEN, so Docker's own
# HEALTHCHECK, Dockhand's health probe, etc. don't need the secret.
UNAUTHENTICATED_PATHS = {"/health", "/ready"}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness check: the process is up and serving HTTP. Does not call Radarr."""
    return JSONResponse({"status": "ok"})


def _check_api_version() -> dict:
    """Compare RADARR_API_VERSION against what Radarr's own /api discovery
    endpoint reports as current/deprecated. Best-effort: a failure here
    (e.g. an old Radarr without this endpoint) doesn't fail /ready on its
    own — only a version Radarr no longer serves at all does."""
    try:
        response = discovery_client.get("/api")
        response.raise_for_status()
        info = response.json()
    except httpx.HTTPError:
        return {"checked": False}

    current = info.get("current")
    deprecated = info.get("deprecated", [])
    supported = RADARR_API_VERSION == current or RADARR_API_VERSION in deprecated
    return {
        "checked": True,
        "configured": RADARR_API_VERSION,
        "current": current,
        "deprecated": deprecated,
        "supported": supported,
    }


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> Response:
    """Readiness check: RADARR_URL is reachable and RADARR_API_KEY is valid."""
    try:
        response = client.get("/system/status", timeout=5)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        reason = "invalid Radarr API key" if status_code == 401 else f"Radarr returned HTTP {status_code}"
        return JSONResponse(
            {"status": "error", "reachable": True, "authenticated": status_code != 401, "error": reason},
            status_code=503,
        )
    except httpx.RequestError as error:
        return JSONResponse(
            {
                "status": "error",
                "reachable": False,
                "authenticated": False,
                "error": f"cannot reach Radarr at {RADARR_URL}: {error}",
            },
            status_code=503,
        )

    api_version = _check_api_version()
    if api_version["checked"] and not api_version["supported"]:
        return JSONResponse(
            {
                "status": "error",
                "reachable": True,
                "authenticated": True,
                "apiVersion": api_version,
                "error": (
                    f"Radarr no longer serves API {RADARR_API_VERSION!r} "
                    f"(current: {api_version['current']!r}, deprecated: {api_version['deprecated']!r}); "
                    "set RADARR_API_VERSION to match"
                ),
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "status": "ok",
            "reachable": True,
            "authenticated": True,
            "radarr": {"url": RADARR_URL, "version": response.json().get("version")},
            "apiVersion": api_version,
        }
    )


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <MCP_AUTH_TOKEN>` on every request except
    the health/readiness endpoints, which are meant to be publicly pollable."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in UNAUTHENTICATED_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, MCP_AUTH_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    """Build the ASGI app (routes + auth middleware). Split out from __main__ so
    tests can exercise the real, fully-wired app without going through uvicorn."""
    app = mcp.streamable_http_app(host=MCP_HOST)

    if MCP_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware)
        print("Auth enabled: Authorization: Bearer <token> required", file=sys.stderr)
    else:
        print("WARNING: MCP_AUTH_TOKEN not set — server is open to anyone who can reach it", file=sys.stderr)

    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT)
