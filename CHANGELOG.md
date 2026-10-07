# Changelog

Changes relative to [Rello/nextcloud-dynamic-mcp-server](https://github.com/Rello/nextcloud-dynamic-mcp-server) at commit `bc2c042`.

## Migrated to MCP `2026-07-28` (SDK `mcp` 2.x)

The upstream targets the pre-2.0 SDK and the stateful handshake protocol. This fork moves to the stateless core:

- Handlers move from `@server.list_tools()` decorators to `Server(on_list_tools=..., on_call_tool=...)` constructor parameters
- Results are constructed explicitly rather than auto-wrapped
- The per-request `contextvars` header shim and hand-rolled ASGI wrapper are gone; handlers read `ctx.request.headers` directly
- `StreamableHTTPSessionManager` plus a manual Starlette app becomes `server.streamable_http_app()`
- `tools/list` carries the `ttlMs` / `cacheScope` hints SEP-2549 requires
- `server/discover` is provided by the SDK
- Outbound HTTP moves from `httpx` to `httpx2`

Older handshake-protocol clients still work; the transport routes by the `MCP-Protocol-Version` header.

## Catalogue replaces the per-operation tool list

Upstream registers one MCP tool per discovered operation. On a fully-loaded instance that is ~545 tools and ~101,000 tokens of `inputSchema` in every client's context, permanently, for a handful of actual calls. Roughly a third of it was not even schema: `dynamic_tool_description` prefixed every tool with the same two fixed sentences, ~98,000 characters of identical text across the list.

This fork publishes four instead - `find` / `describe` / `call` plus the status tool - and searches the catalogue on demand. Measured against the same instance: ~760 tokens, 99.2% less. See [Why Not One Tool Per Operation](README.md#why-a-catalogue-instead-of-one-tool-per-operation).

**Breaking:** operation names are unchanged, but they are no longer MCP tool names. A client that called `collectives_page_create` directly now calls `nextcloud_call_operation` with `{"name": "collectives_page_create", "arguments": {...}}`, and per-tool permission allowlists need rewriting against the fixed tool names.

## File contents, which discovery cannot reach

`ocs_api_viewer` describes the OCS API, and the OCS API serves metadata about files but never their bytes. Upstream therefore cannot read or write a file at all: `collectives_page_get` reports a page's size and filename, `files` exposes sixteen operations, and none of them returns content. For a Collectives instance that means the whole point of the knowledge base - the pages - is unreachable.

Added five WebDAV operations - read, write, list, create folder, delete - speaking with the caller's own credentials. They are generic file access rather than Collectives-aware, so the server keeps its defining property of having no per-app integration code. Several of their behaviours were corrected only after testing against a real instance; see [WebDAV](docs/webdav.md).

## Calendar access, which discovery cannot reach either

Calendars are CalDAV, not an OCS REST API, so upstream cannot list a calendar, read an event, or create/update/delete one - the one discovered calendar-adjacent operation only feeds the dashboard's upcoming-events widget. Added eight `caldav_*` operations - list/create/delete calendars, list/get/create/update/delete events - speaking with the caller's own credentials, following RFC 4791/5545. See [Calendar](docs/caldav.md) for what they cover and what has not been verified against a live instance.

## Security fixes

- **Session token leak.** Proxied responses returned `dict(response.headers)`, which includes the `Set-Cookie` session passphrase Nextcloud issues on every Basic-auth request - putting a live session token into the MCP conversation. Now filtered to a safe allowlist.
- **Auth header override.** An OpenAPI header parameter named `Authorization` became a tool argument that overwrote the caller's credentials. Now filtered alongside `OCS-APIRequest`.
- **Open CORS.** `allow_origins` was pinned to `["*"]`. Now driven by `CORS_ALLOW_ORIGINS`, closed by default.
- **Health endpoint disclosure.** `GET /` returned the internal Nextcloud URL, the API-viewer URL, the installed-app inventory, and raw discovery error text to any origin. Trimmed to non-sensitive fields.
- **Unauthenticated discovery.** Execution was credentialed from the start, but nothing else was. `nextcloud_discovery_status` was dispatched before any credential check, so an anonymous caller got the internal Nextcloud URL and the full app inventory - the very fields `GET /` had just been trimmed of, which made that trimming pointless. Searching the catalogue was open too, `{"refresh": true}` let anyone turn one request into a full re-discovery against Nextcloud using the server's own credentials, and because the unknown-operation branch ran before the credential check its `did_you_mean` list could be used to enumerate operations one guess at a time. All four are now behind the caller's credentials.

## Compatibility fixes

- **Dangling `$ref` broke strict MCP clients.** Nextcloud's OpenAPI documents use `$ref: "#/components/schemas/X"` for recursive or reused fields (e.g. `files_template_create`'s `templateFields`, `spreed_room_create_room`'s `participants`, `tables_api_tables_create_from_scheme`'s `columns`/`views`). The old code copied these `$ref` pointers verbatim into each tool's `inputSchema`, but an MCP client only ever receives that one tool's schema - never the surrounding `components` section - so the pointer resolved to nothing. Claude Code tolerates unresolvable `$ref`s; opencode does not and fails to build a parser for the whole tool list. `build_input_schema` now inlines every `$ref` against the source document before publishing the schema, with cycle/depth guards (`MAX_REF_DEPTH`) in case a future Nextcloud release introduces a self-referential schema. This is generic JSON-Pointer resolution, not special-cased per app or operation - any `$ref` Nextcloud's OpenAPI output produces, now or after an upstream API change, is handled the same way.

## Reliability fixes

- **Discovery could stick permanently.** A failed discovery was cached forever, so a server that started before Nextcloud was reachable stayed toolless until manually restarted. Failures now expire after `DISCOVERY_RETRY_SECONDS`, guarded by a lock so retries cannot stampede, and `nextcloud_discovery_status` accepts `{"refresh": true}`.
- **`stdio` could never execute a tool.** Every call demanded HTTP credential headers, which `stdio` has no way to supply - so the advertised `stdio` transport was execution-dead. It now uses the configured account, which is the sole local caller. HTTP keeps the strict no-fallback rule.
- **Container start command was inconsistent.** The Dockerfile ran `uvicorn main:app` while Compose overrode it with `python main.py`. Running the image directly therefore ignored `MCP_TRANSPORT`, `MCP_HOST`, and `MCP_PORT`, and `MCP_TRANSPORT=stdio` silently did nothing. Both now use `python main.py`.

## Added

- `test_main.py` - test suite
- `.gitignore`, `requirements-dev.txt`
