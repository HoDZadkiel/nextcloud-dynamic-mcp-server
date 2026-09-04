# Dynamic MCP Server for Nextcloud

Exposes a live Nextcloud instance as an MCP server, reflecting whatever apps that instance actually has installed.

Instead of shipping a fixed tool list, this server queries the Nextcloud `ocs_api_viewer` app at startup, reads the OpenAPI description of each installed app, and turns those operations into a searchable catalogue. Point it at a Nextcloud with 28 apps enabled and all ~545 operations those apps expose become callable - no per-app integration code.

The catalogue is published as four fixed tools rather than one tool per operation, so a client spends about 750 tokens of context on this server instead of ~101,000.

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
- Builds a searchable operation catalogue from the discovered OpenAPI documents
- Proxies calls back to the real Nextcloud REST endpoints
- Uses server-side credentials for discovery and per-caller credentials for execution
- Supports both `streamable-http` and `stdio` transports

Operations are named from the Nextcloud app id plus the OpenAPI operation id:

```text
files_sharing_get_shares
provisioning_api_create_user
dav_upcoming_events_get_events
```

## Tools

The server publishes four tools, whatever the connected instance has installed:

- **`nextcloud_find_operations`** - search the catalogue by free text and/or app id. Returns each match's name, HTTP method, path and summary. For a credentialed caller its own description also carries the app index (`collectives:84, spreed:142, ...`), so a client knows what exists before searching.
- **`nextcloud_describe_operations`** - the full JSON Schema for one or more operations, so their arguments can be filled in. Takes a list, so a whole task's operations can be fetched in one call.
- **`nextcloud_call_operation`** - execute one operation by name with an `arguments` object.
- **`nextcloud_discovery_status`** - auth mode, operation count, and last refresh state. A credentialed caller also gets the connected instance URL, the discovered-app inventory and the raw discovery error. Pass `{"refresh": true}` (credentials required) to re-run discovery first.

`find`, `describe` and `call` all require the caller's credentials; see [Authentication](#authentication).

A typical first use is `find` → `describe` → `call`. An unknown or near-miss operation name comes back with a `did_you_mean` list rather than an error, so a wrong guess costs one round trip instead of a failed task.

### Why Not One Tool Per Operation

An instance with every app enabled discovers ~545 operations. Publishing those as ~545 MCP tools puts their full input schemas into the context of every client on every session - measured against a live instance, 403,875 characters, roughly **101,000 tokens**, before a single call is made. No session uses more than a handful of them.

The four-tool surface costs **~750 tokens**, a 99.3% reduction, and a realistic `find` + `describe` round trip adds ~310 tokens. Ten operations looked up on demand still cost an order of magnitude less than the old tool list.

The trade is one extra round trip before an unfamiliar operation, and no client-side schema validation on `nextcloud_call_operation` - the `arguments` object is passed through as given, so `describe` is what keeps a call well-formed.

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

It deliberately does **not** return the Nextcloud URL, the installed-app inventory, or discovery error text. This endpoint is reachable cross-origin and those fields describe an internal instance; call the `nextcloud_discovery_status` tool **with credentials** for the full picture.

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

Discovery is gated to the same degree. The catalogue names every app the instance has installed and every API path it exposes, so `nextcloud_find_operations` and `nextcloud_describe_operations` reject an uncredentialed caller exactly as `nextcloud_call_operation` does, and `nextcloud_discovery_status` withholds the instance URL and app inventory. `tools/list` itself stays open, because a client that cannot list tools cannot connect at all - what it returns to an anonymous caller is four tool names and no instance detail.

Because MCP clients configure these as transport headers, they are sent on every request including `tools/list`; a client that cannot send them could not execute anything anyway.

Over `stdio` there are no HTTP headers and the process serves exactly one local client, so the configured account *is* that caller's account.

Proxied responses return only a safe subset of upstream response headers (`content-type`, `content-length`, `etag`, `last-modified`, `location`, `retry-after`). Nextcloud answers every Basic-auth request with a `Set-Cookie` session passphrase; forwarding it would drop a live session token into the MCP client's conversation history.

## How Discovery Works

At startup the server:

1. Calls `GET /apps/ocs_api_viewer/apps`
2. Loads each app's OpenAPI document from `GET /apps/ocs_api_viewer/apps/{appId}`
3. Builds an MCP input schema from each operation's parameters and request body, inlining any `$ref` against that document's `components` so the schema is self-contained
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

Then call `nextcloud_discovery_status` from your MCP client and confirm the app inventory matches your enabled apps, and `nextcloud_find_operations` with a term like `share` to confirm the catalogue is populated.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

Covers schema generation (including `$ref` inlining), credential handling, discovery retry, catalogue search and description, and the `2026-07-28` protocol surface (`server/discover`, cache hints, per-request auth) against the in-process ASGI app.

**Not covered:** proxying against a real Nextcloud instance, and any write-path operation. The catalogue includes a large number of `POST`/`PUT`/`DELETE` operations that have not been exercised - validate those against a test instance before relying on them.

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

### Catalogue replaces the per-operation tool list

Upstream registers one MCP tool per discovered operation. On a fully-loaded instance that is ~545 tools and ~101,000 tokens of `inputSchema` in every client's context, permanently, for a handful of actual calls. Roughly a third of it was not even schema: `dynamic_tool_description` prefixed every tool with the same two fixed sentences, ~98,000 characters of identical text across the list.

This fork publishes four tools instead - `find` / `describe` / `call` plus the status tool - and searches the catalogue on demand. Measured against the same instance: ~750 tokens, 99.3% less. See [Why Not One Tool Per Operation](#why-not-one-tool-per-operation).

**Breaking:** operation names are unchanged, but they are no longer MCP tool names. A client that called `collectives_page_create` directly now calls `nextcloud_call_operation` with `{"name": "collectives_page_create", "arguments": {...}}`, and per-tool permission allowlists need rewriting against the four tool names.

### Security fixes

- **Session token leak.** Proxied responses returned `dict(response.headers)`, which includes the `Set-Cookie` session passphrase Nextcloud issues on every Basic-auth request - putting a live session token into the MCP conversation. Now filtered to a safe allowlist.
- **Auth header override.** An OpenAPI header parameter named `Authorization` became a tool argument that overwrote the caller's credentials. Now filtered alongside `OCS-APIRequest`.
- **Open CORS.** `allow_origins` was pinned to `["*"]`. Now driven by `CORS_ALLOW_ORIGINS`, closed by default.
- **Health endpoint disclosure.** `GET /` returned the internal Nextcloud URL, the API-viewer URL, the installed-app inventory, and raw discovery error text to any origin. Trimmed to non-sensitive fields.
- **Unauthenticated discovery.** Execution was credentialed from the start, but nothing else was. `nextcloud_discovery_status` was dispatched before any credential check, so an anonymous caller got the internal Nextcloud URL and the full app inventory - the very fields `GET /` had just been trimmed of, which made that trimming pointless. Searching the catalogue was open too, `{"refresh": true}` let anyone turn one request into a full re-discovery against Nextcloud using the server's own credentials, and because the unknown-operation branch ran before the credential check its `did_you_mean` list could be used to enumerate operations one guess at a time. All four are now behind the caller's credentials.

### Compatibility fixes

- **Dangling `$ref` broke strict MCP clients.** Nextcloud's OpenAPI documents use `$ref: "#/components/schemas/X"` for recursive or reused fields (e.g. `files_template_create`'s `templateFields`, `spreed_room_create_room`'s `participants`, `tables_api_tables_create_from_scheme`'s `columns`/`views`). The old code copied these `$ref` pointers verbatim into each tool's `inputSchema`, but an MCP client only ever receives that one tool's schema - never the surrounding `components` section - so the pointer resolved to nothing. Claude Code tolerates unresolvable `$ref`s; opencode does not and fails to build a parser for the whole tool list. `build_input_schema` now inlines every `$ref` against the source document before publishing the schema, with cycle/depth guards (`MAX_REF_DEPTH`) in case a future Nextcloud release introduces a self-referential schema. This is generic JSON-Pointer resolution, not special-cased per app or operation - any `$ref` Nextcloud's OpenAPI output produces, now or after an upstream API change, is handled the same way.

### Reliability fixes

- **Discovery could stick permanently.** A failed discovery was cached forever, so a server that started before Nextcloud was reachable stayed toolless until manually restarted. Failures now expire after `DISCOVERY_RETRY_SECONDS`, guarded by a lock so retries cannot stampede, and `nextcloud_discovery_status` accepts `{"refresh": true}`.
- **`stdio` could never execute a tool.** Every call demanded HTTP credential headers, which `stdio` has no way to supply - so the advertised `stdio` transport was execution-dead. It now uses the configured account, which is the sole local caller. HTTP keeps the strict no-fallback rule.
- **Container start command was inconsistent.** The Dockerfile ran `uvicorn main:app` while Compose overrode it with `python main.py`. Running the image directly therefore ignored `MCP_TRANSPORT`, `MCP_HOST`, and `MCP_PORT`, and `MCP_TRANSPORT=stdio` silently did nothing. Both now use `python main.py`.

### Added

- `test_main.py` - 38 tests
- `.gitignore`, `requirements-dev.txt`
- `DEVLOG.md` - change log and open items
