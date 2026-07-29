# Dynamic MCP Server for Nextcloud

Exposes a live Nextcloud instance as an MCP server, reflecting whatever apps that instance actually has installed.

Instead of shipping a fixed tool list, this server queries the Nextcloud `ocs_api_viewer` app at startup, reads the OpenAPI description of each installed app, and turns those operations into MCP tools dynamically. Point it at a Nextcloud with 28 apps enabled and you get the ~544 tools those apps expose - no per-app integration code.

Speaks MCP protocol revision `2026-07-28` (the stateless core) and still answers the older handshake revisions for clients that have not migrated.

---

## Attribution

This is a fork of **[Rello/nextcloud-dynamic-mcp-server](https://github.com/Rello/nextcloud-dynamic-mcp-server)** by [@Rello](https://github.com/Rello), which is where the dynamic-discovery design and the original implementation come from. Credit for the idea and the working foundation belongs there.

The upstream repository **publishes no license file**. Absent a license, default copyright applies and the terms of reuse are the upstream author's to set - this fork does not add a license of its own, and downstream users should take that up with upstream rather than assume permission from this repository.

See [What This Fork Changes](#what-this-fork-changes) for the delta.

---

## What The Server Does

- Connects to the Nextcloud instance defined by `NEXTCLOUD_URL`
- Reads installed app APIs from `NEXTCLOUD_URL/apps/ocs_api_viewer`
- Builds MCP tools dynamically from the discovered OpenAPI operations
- Proxies tool calls back to the real Nextcloud REST endpoints
- Uses server-side credentials for discovery and per-caller credentials for execution
- Supports both `streamable-http` and `stdio` transports

Dynamic tools are named from the Nextcloud app id plus the OpenAPI operation id:

```text
files_sharing_get_shares
provisioning_api_create_user
dav_upcoming_events_get_events
```

One built-in tool is always present:

- **`nextcloud_discovery_status`** - the connected instance, auth mode, discovered apps, tool count, and last refresh/error state. Pass `{"refresh": true}` to re-run discovery first.

## Quick Start

Requires Docker, a reachable Nextcloud, the `ocs_api_viewer` app enabled on it, and a Nextcloud username plus app token.

Put your instance URL and credentials into `docker-compose.yml`, then:

```bash
docker compose up -d --build
```

The server comes up at `http://localhost:8000/` (health) and `http://localhost:8000/mcp` (MCP).

## Endpoints

### `GET /`

Health endpoint. Returns the server name and version, the MCP path, whether discovery credentials are configured, whether the last discovery succeeded, and the app/tool counts.

It deliberately does **not** return the Nextcloud URL, the installed-app inventory, or discovery error text. This endpoint is reachable cross-origin and those fields describe an internal instance; call the `nextcloud_discovery_status` tool for the full picture.

```bash
curl http://localhost:8000/
```

### `POST /mcp`

The MCP endpoint for `streamable-http` clients.

## Configuration

Entirely environment variables.

| Variable | Default | Description |
|---|---|---|
| `NEXTCLOUD_URL` | `http://nc31-app-1:80` | Base URL of the target Nextcloud instance |
| `NEXTCLOUD_USERNAME` | unset | Username used for startup discovery (and for `stdio` execution) |
| `NEXTCLOUD_APP_TOKEN` | unset | App token used for startup discovery (and for `stdio` execution) |
| `MCP_HOST` | `0.0.0.0` | Bind host for HTTP mode |
| `MCP_PORT` | `8000` | Bind port for HTTP mode |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` or `stdio` |
| `DISCOVERY_TIMEOUT_SECONDS` | `30` | Timeout for discovery and proxied requests |
| `DISCOVERY_RETRY_SECONDS` | `60` | How long a failed discovery is cached before it is retried |
| `TOOL_LIST_TTL_MS` | `300000` | `ttlMs` cache hint sent with `tools/list` results |
| `CORS_ALLOW_ORIGINS` | unset | Comma-separated browser origins allowed to read responses. `*` allows all |
| `LOG_LEVEL` | `INFO` | Python log level |
| `DEBUG` | unset | `true` enables Starlette debug mode |

### Browser Origins

`CORS_ALLOW_ORIGINS` only affects MCP clients running **inside a browser**. CORS is enforced by browsers, so command-line and native clients - Codex, Claude Code, anything using an HTTP library - ignore this setting entirely and need no configuration.

Unset means no cross-origin page can read this server's responses. Set it only if you have a browser-based client.

## Authentication

Two credential paths, deliberately separated:

| Path | Credentials | Used for |
|---|---|---|
| Discovery | `NEXTCLOUD_USERNAME` / `NEXTCLOUD_APP_TOKEN` | Reading the API catalogue at startup |
| Execution (HTTP) | `X-Nextcloud-Username` / `X-Nextcloud-AppToken` request headers | Every proxied tool call |
| Execution (stdio) | `NEXTCLOUD_USERNAME` / `NEXTCLOUD_APP_TOKEN` | Every proxied tool call |

Over HTTP the server **never** falls back to the discovery account for execution. A tool call without credential headers is rejected, so one shared server URL can never let one caller act as another. This is what makes a single deployment safe for a team: everyone points at the same URL and authenticates as themselves.

Over `stdio` there are no HTTP headers and the process serves exactly one local client, so the configured account *is* that caller's account.

Proxied responses return only a safe subset of upstream response headers (`content-type`, `content-length`, `etag`, `last-modified`, `location`, `retry-after`). Nextcloud answers every Basic-auth request with a `Set-Cookie` session passphrase; forwarding it would drop a live session token into the MCP client's conversation history.

## How Discovery Works

At startup the server:

1. Calls `GET /apps/ocs_api_viewer/apps`
2. Loads each app's OpenAPI document from `GET /apps/ocs_api_viewer/apps/{appId}`
3. Builds an MCP input schema from each operation's parameters and request body
4. Registers the operation as a callable MCP tool

Apps whose OpenAPI document fails to load are skipped with a warning; the rest still register.

If discovery fails entirely the server still starts and reports the error through `nextcloud_discovery_status`. Failures expire after `DISCOVERY_RETRY_SECONDS`, so a server that started before Nextcloud was reachable recovers on its own. `nextcloud_discovery_status` with `{"refresh": true}` forces an immediate retry.

## Client Configuration

### Codex

```bash
codex mcp add nextcloud-live --url http://localhost:8000/mcp
```

Equivalent `~/.codex/config.toml`:

```toml
[mcp_servers.nextcloud-live]
url = "http://localhost:8000/mcp"
http_headers = { X-Nextcloud-Username = "NEXTCLOUD_USERNAME", X-Nextcloud-AppToken = "NEXTCLOUD_APP_TOKEN" }
```

### Claude Code

```bash
claude mcp add --transport http nextcloud-live http://localhost:8000/mcp
```

Project-scoped `.mcp.json`, taking each developer's own credentials from their environment:

```json
{
  "mcpServers": {
    "nextcloud-live": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "X-Nextcloud-Username": "${NEXTCLOUD_USERNAME}",
        "X-Nextcloud-AppToken": "${NEXTCLOUD_APP_TOKEN}"
      }
    }
  }
}
```

## Smoke Checks

```bash
curl http://localhost:8000/
```

```bash
docker compose logs -f mcp
```

Then call `nextcloud_discovery_status` from your MCP client and confirm the tool list includes operations from your enabled apps.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

Covers schema generation, credential handling, discovery retry, and the `2026-07-28` protocol surface (`server/discover`, cache hints, per-request auth) against the in-process ASGI app.

**Not covered:** proxying against a real Nextcloud instance, and any write-path operation. The tool catalogue includes a large number of `POST`/`PUT`/`DELETE` operations that have not been exercised - validate those against a test instance before relying on them.

## What This Fork Changes

Relative to [Rello/nextcloud-dynamic-mcp-server](https://github.com/Rello/nextcloud-dynamic-mcp-server) at commit `bc2c042`.

### Migrated to MCP `2026-07-28` (SDK `mcp` 2.x)

The upstream targets the pre-2.0 SDK and the stateful handshake protocol. This fork moves to the stateless core:

- Handlers move from `@server.list_tools()` decorators to `Server(on_list_tools=..., on_call_tool=...)` constructor parameters
- Results are constructed explicitly rather than auto-wrapped
- The per-request `contextvars` header shim and hand-rolled ASGI wrapper are gone; handlers read `ctx.request.headers` directly
- `StreamableHTTPSessionManager` plus a manual Starlette app becomes `server.streamable_http_app()`
- `tools/list` carries the `ttlMs` / `cacheScope` hints SEP-2549 requires
- `server/discover` is provided by the SDK
- Outbound HTTP moves from `httpx` to `httpx2`

Older handshake-protocol clients still work; the transport routes by the `MCP-Protocol-Version` header.

### Security fixes

- **Session token leak.** Proxied responses returned `dict(response.headers)`, which includes the `Set-Cookie` session passphrase Nextcloud issues on every Basic-auth request - putting a live session token into the MCP conversation. Now filtered to a safe allowlist.
- **Auth header override.** An OpenAPI header parameter named `Authorization` became a tool argument that overwrote the caller's credentials. Now filtered alongside `OCS-APIRequest`.
- **Open CORS.** `allow_origins` was pinned to `["*"]`. Now driven by `CORS_ALLOW_ORIGINS`, closed by default.
- **Health endpoint disclosure.** `GET /` returned the internal Nextcloud URL, the API-viewer URL, the installed-app inventory, and raw discovery error text to any origin. Trimmed to non-sensitive fields.

### Reliability fixes

- **Discovery could stick permanently.** A failed discovery was cached forever, so a server that started before Nextcloud was reachable stayed toolless until manually restarted. Failures now expire after `DISCOVERY_RETRY_SECONDS`, guarded by a lock so retries cannot stampede, and `nextcloud_discovery_status` accepts `{"refresh": true}`.
- **`stdio` could never execute a tool.** Every call demanded HTTP credential headers, which `stdio` has no way to supply - so the advertised `stdio` transport was execution-dead. It now uses the configured account, which is the sole local caller. HTTP keeps the strict no-fallback rule.
- **Container start command was inconsistent.** The Dockerfile ran `uvicorn main:app` while Compose overrode it with `python main.py`. Running the image directly therefore ignored `MCP_TRANSPORT`, `MCP_HOST`, and `MCP_PORT`, and `MCP_TRANSPORT=stdio` silently did nothing. Both now use `python main.py`.

### Added

- `test_main.py` - 18 tests
- `.gitignore`, `requirements-dev.txt`
- `DEVLOG.md` - change log and open items
