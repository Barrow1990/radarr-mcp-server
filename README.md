# radarr-mcp-server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that
connects to [Radarr](https://radarr.video), packaged for Docker.

It runs as a standing network service (streamable-http transport, not stdio),
so any MCP client on your internal network can connect to
`http://<host>:<port>/mcp` — the container isn't spawned per-client, and
container lifecycle/updates can be handed off to a tool like
[Dockhand](https://dockhand.pro).

## Tools

| Tool | Description |
|---|---|
| `list_movies` | List movies in the library, optionally filtered by title |
| `movie_details` | Full details for one movie by ID |
| `missing_movies` | Monitored movies that don't have a file yet |
| `lookup_movie` | Search for new movies by title (does not add them) |
| `search_movie` | Trigger a search for a movie already in the library |
| `system_status` | Radarr system status, disk space, and health checks |

`search_movie` is the only tool that changes state in Radarr (it kicks off a
real search/download). Everything else is read-only.

## Health endpoints

Two plain HTTP endpoints, reachable without `MCP_AUTH_TOKEN` (so Docker's
`HEALTHCHECK`, Dockhand, or any other monitor can poll them without the
secret):

| Endpoint | Checks | Healthy | Unhealthy |
|---|---|---|---|
| `GET /health` | The process is up and serving HTTP. Does **not** call Radarr. | `200 {"status": "ok"}` | (doesn't respond) |
| `GET /ready` | `RADARR_URL` is reachable, `RADARR_API_KEY` is accepted (via Radarr's `/system/status`), *and* `RADARR_API_VERSION` is still an API version Radarr serves (see [API version checking](#api-version-checking)). | `200 {"status": "ok", "reachable": true, "authenticated": true, "radarr": {...}, "apiVersion": {...}}` | `503 {"status": "error", "reachable": ..., "authenticated": ..., "error": "..."}` |

They're split deliberately: `/health` is what the container's own
`HEALTHCHECK` uses (so a transient Radarr outage doesn't get the container
itself restarted in a loop), while `/ready` is for verifying config — after
changing `RADARR_URL`/`RADARR_API_KEY`, `curl http://<host>:8932/ready` tells
you plainly whether the host is reachable, the key is valid, or both.

## Authentication

Set `MCP_AUTH_TOKEN` (a random shared secret — `openssl rand -hex 32`) and
every request must carry `Authorization: Bearer <token>` or the server
returns `401`. This is checked by a small Starlette middleware in front of
the MCP app, **not** the `mcp` SDK's built-in OAuth support
(`mcp.server.auth`) — that machinery expects a full OAuth authorization
server (issuer/resource metadata, RFC 8414/8707/9068 discovery), which is
unnecessary complexity for one secret shared by trusted LAN clients.

Leave `MCP_AUTH_TOKEN` unset and the server runs with **no auth** — anything
that can reach `http://<host>:<port>/mcp` can call every tool, including
`search_movie`. The server logs a warning on startup when it's running this
way. Either way, the trust boundary is still the network:

- **Do not** publish this port through any reverse proxy, port-forward, or
  anything else reachable from outside your LAN/VLAN — the bearer token
  protects against anyone *on* the network, not against the open internet.
- Bind the compose `ports:` mapping to a specific internal interface (e.g.
  `192.168.1.50:8932:8932`) rather than all interfaces, if you want to be
  stricter about which hosts on your network can reach it at all.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `RADARR_URL` | yes | — | e.g. `http://192.168.1.50:7878` |
| `RADARR_API_KEY` | yes | — | Radarr > Settings > General > API Key |
| `RADARR_API_VERSION` | no | `v3` | Radarr REST API version to call (`/api/<version>/...`) |
| `MCP_HOST` | no | `0.0.0.0` | Interface the server binds to inside the container |
| `MCP_PORT` | no | `8932` | Port the server listens on |
| `MCP_AUTH_TOKEN` | no | — | Shared secret required as `Authorization: Bearer <token>`. Unset = no auth (see above) |

### API version checking

Radarr shares its underlying HTTP framework with Sonarr (both are Servarr
apps built on the same *arr common codebase), so it exposes the same
unauthenticated, unversioned `GET /api` endpoint reporting which API version
is current and which are deprecated (e.g. `{"current": "v3", "deprecated":
[]}`). `GET /ready` calls it and compares it against `RADARR_API_VERSION`:

- version matches `current`, or is listed under `deprecated` (still served,
  just on notice) → healthy, reported under the response's `apiVersion` key.
- version isn't offered at all any more → `503`, since every tool call
  would otherwise start failing with 404s. Bump `RADARR_API_VERSION` to
  match what Radarr now reports.
- Radarr doesn't have this endpoint (very old versions) or it's
  unreachable → non-fatal, `apiVersion: {"checked": false}`.

This turns a silent break on a Radarr upgrade into a readiness-probe
failure instead.

## Image

Built and pushed to `ghcr.io/barrow1990/radarr-mcp-server` by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) on every push to
`main` that passes tests, tagged `:latest`, `:<commit-sha>`, and
`:radarr-<api-version>` (e.g. `:radarr-v3` — the Radarr API version this
build targets, read out of `server.py`'s `RADARR_API_VERSION` default so it
can't drift from what the code actually calls). `docker-compose.yml` pulls
`:latest` by default; swap in `build: .` there instead if you'd rather build
locally from the `Dockerfile`.

The image is a three-stage build: `builder` compiles dependencies into
`--target=/deps` (all of them, including `cryptography`'s compiled `cffi`
extension, ship musllinux wheels, so this needs no compiler even on alpine);
`prep` starts fresh from `python:3.12-alpine`, drops pip/setuptools/wheel,
strips stdlib pieces this headless server never touches (`tkinter`,
`idlelib`, `lib2to3`, `ensurepip`, ...), adds the non-root `app` user, and
copies in `/deps` and `server.py`; `runtime` then does a single
`COPY --from=prep / /` onto a `scratch` base. That last step matters more
than it looks — a plain `RUN rm -rf` only *hides* files still physically
present in the base image's own layers underneath, so it doesn't shrink a
normal layered image at all; copying the already-trimmed filesystem onto
`scratch` is what actually drops those bytes from what gets pushed.

That takes the published image to roughly **~98MB**. The floor from here is
`mcp`'s own dependency graph: `mcp.server.request_state` unconditionally
imports `cryptography`'s AES-GCM/HKDF (spec-mandated integrity protection for
MCP's `requestState`, not something gated behind JWT/OAuth use), so its
~15MB native extension ships regardless. Dependencies in `requirements.txt`
are pinned to exact versions rather than `>=` ranges, so a routine
`docker build` can't silently pull in a heavier resolution than
the one that was actually tested.

## Running with Docker Compose

```bash
cp .env.example .env   # fill in RADARR_URL / RADARR_API_KEY
docker compose up -d --pull always
```

The server is then reachable at `http://<docker-host>:8932/mcp` from anything
on your internal network.

## Managing with Dockhand

Point Dockhand at `ghcr.io/barrow1990/radarr-mcp-server` and let it track new
tags — this is the registry-pull model Dockhand's image-update tracking
(Grype/Trivy scans, tag tracking, scheduled updates) is actually built around.
The alternative, pointing Dockhand at this repo as a Git-deployed Compose
stack with `build: .`, works too, but syncing new Git commits does **not**
imply rebuilding the image — those are two separate steps for a build-from-
source stack.

**Make the GHCR package public**, or every pull will need `docker login
ghcr.io` with a PAT on each deploy host — a private package by default
requires auth even to `docker pull`, which most homelab boxes won't have
configured.

Set a restart policy of `unless-stopped` (already in `docker-compose.yml`) so
Dockhand-driven restarts and host reboots bring it back up without manual
intervention. The `HEALTHCHECK` in the `Dockerfile` (`GET /health`) drives
Docker's/Dockhand's container health status; use `GET /ready` (see above)
separately if you want to alert on Radarr connectivity specifically rather
than container liveness.

**Environment variables in Dockhand**: `docker-compose.yml` loads
`RADARR_URL`/`RADARR_API_KEY`/`MCP_AUTH_TOKEN` via `env_file: [.env, .env.dockhand]`
(both optional; `.env.dockhand` loads second, so it wins for any key it also
sets). This is deliberate — a Git-deployed stack's `.env` is whatever's
checked out from the repo (i.e. `.env.example`'s placeholders, since real
`.env` is gitignored and not committed), while Dockhand writes the values you
configure in its UI to `.env.dockhand` instead. If you set `RADARR_URL` in
Dockhand's UI and the container is still using a placeholder, check that
Dockhand is actually writing to `.env.dockhand` in the stack directory (not
some other file) and that a rebuild has run since — a synced Git file change
alone doesn't rebuild the image; see `GET /ready` to confirm what's live.

## Connecting a client

### Claude Code

```bash
claude mcp add radarr -s user --transport http http://<docker-host>:8932/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```
(Drop the `--header` flag if you're running with `MCP_AUTH_TOKEN` unset.)

### Claude Desktop

Claude Desktop's built-in config expects a locally-spawned `command`, so for
a network server like this you'll need an HTTP-to-stdio bridge such as
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "radarr": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "http://<docker-host>:8932/mcp",
        "--header", "Authorization: Bearer <MCP_AUTH_TOKEN>"
      ]
    }
  }
}
```

## Running without Docker

```bash
pip install -r requirements.txt
RADARR_URL=http://192.168.1.50:7878 RADARR_API_KEY=your-api-key \
MCP_AUTH_TOKEN=your-shared-secret python server.py
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

- `tests/test_tools.py` — each tool's logic against a mocked Radarr
  (`httpx.MockTransport`, no extra mocking library needed).
- `tests/test_http.py` — `/health`, `/ready`, and the bearer-auth middleware,
  via `server.build_app()` (the exact app `__main__` runs) through Starlette's
  `TestClient`.
- `tests/test_live_radarr.py` — **opt-in** contract tests against a real
  Radarr instance, to catch drift if a Radarr upgrade renames/removes a field
  these tools depend on (`id`, `title`, `hasFile`, `tmdbId`, `version`, ...).
  Skipped by default (no Radarr in CI); run with:
  ```bash
  RUN_LIVE_RADARR_TESTS=1 RADARR_URL=https://radarr.example.com \
  RADARR_API_KEY=<real key> python -m pytest tests/test_live_radarr.py -v
  ```

CI (`.github/workflows/ci.yml`) runs the mocked suite on every push/PR; the
GHCR build only runs after it passes.

## License

MIT
