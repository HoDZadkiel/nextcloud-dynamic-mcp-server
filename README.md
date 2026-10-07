# Dynamic MCP Server for Nextcloud

Exposes a live Nextcloud instance as an MCP server, reflecting whatever apps that instance actually has installed.

At startup the server reads the OpenAPI description of every installed app from Nextcloud's `ocs_api_viewer` app and turns those operations into a searchable catalogue. An instance with 28 apps exposes ~545 operations - no per-app integration code. The catalogue is published as **four fixed tools** (~760 tokens of client context instead of ~101,000).

Speaks MCP protocol revision `2026-07-28`, and still answers older handshake revisions.

> [!WARNING]
> The CalDAV operations have been exercised against a live Nextcloud, but most other write operations (`POST`/`PUT`/`DELETE`) in the catalogue are untested. Try them on a test instance first. See [Known limitations](#known-limitations).

This is a fork of [Rello/nextcloud-dynamic-mcp-server](https://github.com/Rello/nextcloud-dynamic-mcp-server); see [Attribution](#attribution).

## Quick Start

### 1. Prepare Nextcloud

1. Install and enable the **OCS API Viewer** (`ocs_api_viewer`) app from the Nextcloud App Store. Discovery fails without it.
2. Create an app token: **Personal settings → Security → Devices & sessions → Create new app password**. Use a regular user's token, not your main password.

### 2. Configure

Edit `docker-compose.yml`:

```yaml
NEXTCLOUD_URL: "https://nextcloud.example.com"   # your instance, reachable from the container
NEXTCLOUD_USERNAME: "your-username"
NEXTCLOUD_APP_TOKEN: "your-app-token"
```

The compose file attaches to an external Docker network named `testnet`. Create it once, or remove the `networks:` entries if you don't need it:

```bash
docker network create testnet
```

### 3. Run

```bash
docker compose up -d --build
curl http://localhost:8000/        # health check
```

The MCP endpoint is `http://localhost:8000/mcp`.

**Without Docker:**

```bash
pip install -r requirements.txt
NEXTCLOUD_URL=... NEXTCLOUD_USERNAME=... NEXTCLOUD_APP_TOKEN=... python main.py
```

Set `MCP_TRANSPORT=stdio` to run as a local stdio server instead of HTTP.

### 4. Connect a client

Each user sends **their own** Nextcloud credentials as headers, so one shared server URL is safe for a whole team.

**Claude Code** (`.mcp.json`, credentials taken from each developer's environment):

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

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.nextcloud-live]
url = "http://localhost:8000/mcp"
http_headers = { X-Nextcloud-Username = "your-username", X-Nextcloud-AppToken = "your-app-token" }
```

> [!IMPORTANT]
> The token travels in an HTTP header. For anything beyond localhost, put the server behind an HTTPS reverse proxy.

### 5. Verify

Call `nextcloud_discovery_status` from your client and check the app list matches your instance, then `nextcloud_find_operations` with a term like `share`.

## Tools

Four tools, whatever the instance has installed:

| Tool                            | Purpose                                                                                                                                    |
|---------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| `nextcloud_find_operations`     | Search the catalogue by free text and/or app id.                                                                                           |
| `nextcloud_describe_operations` | Full JSON Schema for one or more operations.                                                                                               |
| `nextcloud_call_operation`      | Run one operation by name with an `arguments` object.                                                                                      |
| `nextcloud_discovery_status`    | Auth mode, operation count, last refresh. With credentials: instance URL, app inventory, raw error. `{"refresh": true}` re-runs discovery. |

Typical flow: **find → describe → call**. A wrong or near-miss operation name returns a `did_you_mean` list instead of failing.

Operation names are the app id plus the OpenAPI operation id, e.g. `files_sharing_get_shares`, `provisioning_api_create_user`.

Every tool except `nextcloud_discovery_status` requires the caller's credentials.

### Files and calendars

OCS discovery cannot read file contents or touch calendars, so the catalogue also contains hand-written operations that run with the caller's own credentials:

- `webdav_*` (5 operations) - read, write, list, create folder, delete files. Details: [docs/webdav.md](docs/webdav.md)
- `caldav_*` (8 operations) - list/create/delete calendars; list/get/create/update/delete events. Details: [docs/caldav.md](docs/caldav.md)

### Why a catalogue instead of one tool per operation

Publishing ~545 tools costs ~101,000 tokens of context per session (measured: 403,875 characters of schema), though a session uses only a handful. The four fixed tools cost ~760 tokens; a typical find + describe round trip adds ~310. The trade-off: one extra round trip before an unfamiliar operation, and no client-side validation of `arguments` - `describe` is what keeps a call well-formed.

## Configuration

All settings are environment variables.

| Variable                    | Default                | Description                                                                                                 |
|-----------------------------|------------------------|-------------------------------------------------------------------------------------------------------------|
| `NEXTCLOUD_URL`             | `http://nc31-app-1:80` | Base URL of your Nextcloud. **Always set this** - the default is only a placeholder.                        |
| `NEXTCLOUD_USERNAME`        | unset                  | Account for startup discovery (and for `stdio` execution)                                                   |
| `NEXTCLOUD_APP_TOKEN`       | unset                  | App token for the above                                                                                     |
| `MCP_TRANSPORT`             | `streamable-http`      | `streamable-http` or `stdio`                                                                                |
| `MCP_HOST` / `MCP_PORT`     | `0.0.0.0` / `8000`     | Bind address for HTTP mode                                                                                  |
| `DISCOVERY_TIMEOUT_SECONDS` | `30`                   | Timeout for discovery and proxied requests                                                                  |
| `DISCOVERY_RETRY_SECONDS`   | `60`                   | How long a failed discovery is cached before retrying                                                       |
| `TOOL_LIST_TTL_MS`          | `300000`               | `ttlMs` cache hint on `tools/list`                                                                          |
| `CORS_ALLOW_ORIGINS`        | unset                  | Comma-separated origins for **browser-based** clients (`*` allows all). CLI and native clients ignore CORS. |
| `LOG_LEVEL`                 | `INFO`                 | Python log level                                                                                            |
| `DEBUG`                     | unset                  | `true` enables Starlette debug mode                                                                         |

## Authentication

| Path              | Credentials                                             | Used for                             |
|-------------------|---------------------------------------------------------|--------------------------------------|
| Discovery         | `NEXTCLOUD_USERNAME` / `NEXTCLOUD_APP_TOKEN`            | Reading the API catalogue at startup |
| Execution (HTTP)  | `X-Nextcloud-Username` / `X-Nextcloud-AppToken` headers | Every proxied call                   |
| Execution (stdio) | `NEXTCLOUD_USERNAME` / `NEXTCLOUD_APP_TOKEN`            | Every proxied call                   |

- Over HTTP the server **never** falls back to the discovery account for execution, so callers cannot act as each other.
- `find`, `describe`, `call` and the detailed `status` all reject uncredentialed callers, because the catalogue reveals installed apps and API paths. `tools/list` stays open (clients need it to connect) but shows only fixed tool names.
- Over `stdio` there is one local client, so the configured account is that caller.
- Only a safe subset of upstream response headers is returned; Nextcloud's `Set-Cookie` session token is never forwarded.
- `GET /` (health) deliberately omits the Nextcloud URL, app inventory and error text.

## How Discovery Works

1. `GET /apps/ocs_api_viewer/apps` lists installed apps.
2. Each app's OpenAPI document is loaded from `/apps/ocs_api_viewer/apps/{appId}`.
3. Operations are turned into catalogue entries with self-contained input schemas (`$ref`s inlined).

Apps whose document fails to load are skipped with a warning. If discovery fails entirely the server still starts; failures expire after `DISCOVERY_RETRY_SECONDS`, and `nextcloud_discovery_status` with `{"refresh": true}` forces a retry.

## Troubleshooting

| Symptom                                           | Likely cause                                                                                                                                |
|---------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `nextcloud_discovery_status` reports 0 operations | `ocs_api_viewer` not enabled, wrong `NEXTCLOUD_URL`, or bad discovery credentials. Check the status error and `docker compose logs -f mcp`. |
| Tool call rejected for missing credentials        | Client isn't sending `X-Nextcloud-Username` / `X-Nextcloud-AppToken`.                                                                       |
| `docker compose up` fails on network `testnet`    | Run `docker network create testnet`, or remove the `networks:` entries.                                                                     |
| Container can't reach Nextcloud                   | `NEXTCLOUD_URL` must be reachable from inside the container (not `localhost`; try `http://host.docker.internal:<port>`).                    |
| Browser client blocked                            | Set `CORS_ALLOW_ORIGINS`.                                                                                                                   |

## Known limitations

- CalDAV operations were verified against a live Nextcloud (create/list/get/update/delete for calendars and events, ETag guards, recurring-event windows, cross-calendar listing). They handle `VEVENT` only and don't parse `VTIMEZONE`; event times are returned in UTC.
- Writes to read-only shared calendars, and dated recurrence exceptions (`override_count` > 0), have not been tested.
- Most other `POST`/`PUT`/`DELETE` operations in the catalogue are untested.
- Proxying against a real instance is not covered by the automated tests.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Attribution

Fork of [Rello/nextcloud-dynamic-mcp-server](https://github.com/Rello/nextcloud-dynamic-mcp-server) by [@Rello](https://github.com/Rello), where the dynamic-discovery design and original implementation come from. See [CHANGELOG.md](CHANGELOG.md) for what this fork changes.

The upstream repository **publishes no license**, so default copyright applies. **This repository has no license file either** and does not grant one; take reuse questions up with the upstream author.