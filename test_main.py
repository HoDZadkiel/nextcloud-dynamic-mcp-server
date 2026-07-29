import asyncio
import os
import time

# Port 9 (discard) refuses instantly, so the startup discovery the server
# lifespan kicks off fails fast instead of hanging the suite on a real network.
os.environ.setdefault("NEXTCLOUD_URL", "http://127.0.0.1:9")
os.environ.setdefault("NEXTCLOUD_USERNAME", "discovery-admin")
os.environ.setdefault("NEXTCLOUD_APP_TOKEN", "discovery-token")
os.environ.setdefault("DISCOVERY_TIMEOUT_SECONDS", "1")

import httpx2

import main

PROTOCOL_VERSION = "2026-07-28"


def make_state(**kwargs) -> main.DiscoveryState:
    return main.DiscoveryState(**kwargs)


class FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


# --- response header leakage -------------------------------------------------


def test_safe_response_headers_drops_session_cookie():
    """Nextcloud answers Basic-auth requests with a session cookie. Returning it
    would put a live session token into the MCP client's conversation."""
    headers = httpx2.Headers(
        {
            "content-type": "application/json",
            "set-cookie": "nc_session_id=deadbeef; HttpOnly",
            "authorization": "Basic c2VjcmV0",
            "x-request-id": "abc123",
        }
    )

    result = main.safe_response_headers(headers)

    assert result == {"content-type": "application/json"}


# --- header parameters cannot hijack authentication --------------------------


def test_internal_header_params_are_not_exposed_as_tool_arguments():
    """An OpenAPI header parameter named Authorization would otherwise become a
    tool argument that overwrites the caller's credentials in execute_operation."""
    parameters = [
        {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
        {"name": "OCS-APIRequest", "in": "header", "schema": {"type": "string"}},
        {"name": "X-Custom", "in": "header", "schema": {"type": "string"}},
    ]

    schema, _, _, _, _, _, header_params = main.build_input_schema(parameters, None)

    assert header_params == ["X-Custom"]
    assert "Authorization" not in schema["properties"]
    assert "OCS-APIRequest" not in schema["properties"]


# --- request body shaping ----------------------------------------------------


def test_json_object_body_is_flattened_into_tool_arguments():
    request_body = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"userid": {"type": "string"}, "password": {"type": "string"}},
                    "required": ["userid"],
                }
            }
        },
    }

    schema, body_mode, body_fields, content_type, _, _, _ = main.build_input_schema([], request_body)

    assert body_mode == "flattened_json_object"
    assert body_fields == ["userid", "password"]
    assert content_type == "application/json"
    assert schema["required"] == ["userid"]


def test_body_is_not_flattened_when_a_field_collides_with_a_parameter():
    """Flattening a field that shadows a path/query parameter would silently send
    the wrong value to Nextcloud, so the whole body must stay nested."""
    parameters = [{"name": "userid", "in": "path", "required": True, "schema": {"type": "string"}}]
    request_body = {
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"userid": {"type": "string"}}}
            }
        }
    }

    schema, body_mode, body_fields, _, path_params, _, _ = main.build_input_schema(parameters, request_body)

    assert body_mode == "body"
    assert body_fields == ["body"]
    assert path_params == ["userid"]
    assert schema["properties"]["userid"]["type"] == "string"


def test_flattened_body_sends_only_supplied_fields():
    definition = main.OperationDefinition(
        name="t", app_id="a", app_name="a", method="POST", path="/p", summary="", description="",
        input_schema={}, path_params=[], query_params=[], header_params=[],
        body_mode="flattened_json_object", body_fields=["userid", "password"],
        body_content_type="application/json",
    )

    json_body, form_body, raw_body = main.build_request_body(definition, {"userid": "alice"})

    assert json_body == {"userid": "alice"}
    assert form_body is None and raw_body is None


# --- tool naming -------------------------------------------------------------


def test_tool_name_falls_back_to_method_and_path_without_operation_id():
    assert main.normalize_tool_name("files_sharing", None, "get", "/api/v1/shares") == (
        "files_sharing_get_api_v1_shares"
    )


def test_tool_name_collapses_punctuation_runs():
    assert main.normalize_tool_name("dav", "get--upcoming__events", "get", "/x") == "dav_get_upcoming_events"


# --- discovery retry ---------------------------------------------------------


def test_successful_discovery_is_not_retried():
    main.DISCOVERY_STATE = make_state(last_refresh=main.now_iso(), last_attempt=time.monotonic())
    assert main.discovery_is_current() is True


def test_failed_discovery_is_retried_after_the_backoff_window():
    """A server that starts before Nextcloud is reachable must recover on its own
    instead of staying permanently toolless until someone restarts it."""
    main.DISCOVERY_STATE = make_state(
        last_error="connection refused",
        last_attempt=time.monotonic() - (main.DISCOVERY_RETRY_SECONDS + 1),
    )
    assert main.discovery_is_current() is False


def test_failed_discovery_is_not_retried_inside_the_backoff_window():
    main.DISCOVERY_STATE = make_state(last_error="connection refused", last_attempt=time.monotonic())
    assert main.discovery_is_current() is True


# --- execution credentials ---------------------------------------------------


def test_http_call_without_headers_does_not_fall_back_to_server_credentials():
    """The startup admin account must never execute another caller's tool call."""
    ctx = type("Ctx", (), {"request": FakeRequest({})})()

    auth = main.execution_auth_context(ctx)

    assert auth.auth_header is None
    assert auth.source == "none"


def test_http_call_uses_caller_supplied_headers():
    ctx = type("Ctx", (), {"request": FakeRequest(
        {"x-nextcloud-username": "alice", "x-nextcloud-apptoken": "alice-token"}
    )})()

    auth = main.execution_auth_context(ctx)

    assert auth.auth_header == main.encode_basic_auth("alice", "alice-token")
    assert auth.source == "request_basic_headers"


def test_stdio_call_uses_environment_credentials():
    """stdio carries no HTTP headers and serves exactly one local client, so the
    configured account is that client's own account."""
    ctx = type("Ctx", (), {"request": None})()

    auth = main.execution_auth_context(ctx)

    assert auth.auth_header == main.encode_basic_auth("discovery-admin", "discovery-token")
    assert auth.source == "env_basic"


# --- MCP 2026-07-28 protocol surface ----------------------------------------


async def post_mcp(client, method, params=None, headers=None):
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if headers:
        request_headers.update(headers)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    if params:
        body["params"].update(params)
    response = await client.post("http://test/mcp", json=body, headers=request_headers)
    return response.json()


def run_mcp(coro_factory):
    """Each run gets a fresh app: a StreamableHTTPSessionManager may only be
    started once, so the module-level instance cannot be reused across tests."""

    async def runner():
        mcp_app = main.build_mcp_app()
        async with mcp_app.router.lifespan_context(mcp_app):
            # Overwrite the state the lifespan's startup discovery just produced,
            # so the assertions below do not depend on a reachable Nextcloud.
            main.DISCOVERY_STATE = make_state(
                operations={},
                apps=[],
                last_refresh=main.now_iso(),
                last_attempt=time.monotonic(),
            )
            transport = httpx2.ASGITransport(app=main.wrap_cors(mcp_app))
            async with httpx2.AsyncClient(transport=transport) as client:
                return await coro_factory(client)

    return asyncio.run(runner())


def test_server_discover_advertises_the_2026_07_28_revision():
    """The revision made server/discover mandatory; clients use it to pick a version."""
    result = run_mcp(lambda client: post_mcp(client, "server/discover"))["result"]

    assert PROTOCOL_VERSION in result["supportedVersions"]
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == main.SERVER_NAME


def test_tools_list_carries_cache_hints():
    """SEP-2549 requires ttlMs/cacheScope so clients can cache instead of polling."""
    result = run_mcp(lambda client: post_mcp(client, "tools/list"))["result"]

    assert result["ttlMs"] == main.TOOL_LIST_TTL_MS
    assert result["cacheScope"] == "private"
    assert [tool["name"] for tool in result["tools"]] == [main.STATUS_TOOL_NAME]


def test_status_tool_reports_caller_credentials_from_request_headers():
    """Proves per-request auth survives the stateless rewrite: no session, the
    credentials must come off the HTTP request the tool call arrived on."""
    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.STATUS_TOOL_NAME, "arguments": {}},
            {
                "Mcp-Name": main.STATUS_TOOL_NAME,
                "X-Nextcloud-Username": "alice",
                "X-Nextcloud-AppToken": "alice-token",
            },
        )
    )["result"]

    assert result["structuredContent"]["request_auth_configured"] is True
    assert result["structuredContent"]["request_auth_source"] == "request_basic_headers"


def test_unknown_tool_is_rejected_as_invalid_params():
    error = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": "does_not_exist", "arguments": {}},
            {"Mcp-Name": "does_not_exist"},
        )
    )["error"]

    assert error["code"] == -32602


def test_healthcheck_does_not_leak_the_nextcloud_url():
    """GET / is reachable cross-origin; it must not disclose the internal
    instance URL or the installed-app inventory."""

    async def fetch(client):
        response = await client.get("http://test/")
        return response.json()

    payload = run_mcp(fetch)

    assert "nextcloud_url" not in payload
    assert "apps" not in payload
    assert payload["tool_count"] == 0
