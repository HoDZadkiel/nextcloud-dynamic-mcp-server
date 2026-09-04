import asyncio
import base64
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

META_TOOL_NAMES = [
    main.STATUS_TOOL_NAME,
    main.FIND_TOOL_NAME,
    main.DESCRIBE_TOOL_NAME,
    main.CALL_TOOL_NAME,
]

AUTH_HEADERS = {
    "X-Nextcloud-Username": "alice",
    "X-Nextcloud-AppToken": "alice-token",
}


def call_headers(tool_name, authenticated=True):
    headers = {"Mcp-Name": tool_name}
    if authenticated:
        headers.update(AUTH_HEADERS)
    return headers


def make_state(**kwargs) -> main.DiscoveryState:
    return main.DiscoveryState(**kwargs)


def make_operation(name, app_id="collectives", summary="", path="/x", **kwargs) -> main.OperationDefinition:
    defaults = dict(
        app_name=app_id,
        method="GET",
        description=summary,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        path_params=[],
        query_params=[],
        header_params=[],
        body_mode="none",
        body_fields=[],
        body_content_type=None,
    )
    defaults.update(kwargs)
    return main.OperationDefinition(
        name=name, app_id=app_id, summary=summary, path=path, **defaults
    )


def catalogue(*definitions) -> dict[str, main.OperationDefinition]:
    return {definition.name: definition for definition in definitions}


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


# --- schema $ref resolution --------------------------------------------------
#
# An MCP client only ever receives a tool's inputSchema, never the OpenAPI
# document it was extracted from. A surviving `#/components/schemas/...` pointer
# is therefore unresolvable on the client side, and strict clients (opencode)
# fail the whole tool list over it.


def json_of(schema) -> str:
    import json

    return json.dumps(schema)


def test_nested_ref_is_inlined_into_tool_schema():
    """files_template_create's real body: templateFields[].$ref -> TemplateField."""
    document = {
        "components": {
            "schemas": {
                "TemplateField": {
                    "type": "object",
                    "required": ["index", "type"],
                    "properties": {"index": {"type": "string"}, "type": {"type": "string"}},
                }
            }
        }
    }
    request_body = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["filePath"],
                    "properties": {
                        "filePath": {"type": "string"},
                        "templateFields": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TemplateField"},
                        },
                    },
                }
            }
        },
    }

    schema, body_mode, body_fields, _, _, _, _ = main.build_input_schema([], request_body, document)

    assert body_mode == "flattened_json_object"
    assert body_fields == ["filePath", "templateFields"]
    assert schema["properties"]["templateFields"]["items"]["properties"]["index"] == {"type": "string"}
    assert "$ref" not in json_of(schema)


def test_ref_siblings_are_kept_alongside_the_resolved_target():
    """spreed's `parts` carries $ref plus default/description on the same node;
    dropping the siblings would lose the argument's documentation."""
    document = {"components": {"schemas": {"Parts": {"type": "array", "items": {"type": "object"}}}}}
    request_body = {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "parts": {
                            "$ref": "#/components/schemas/Parts",
                            "default": [],
                            "description": "New parts",
                        },
                    },
                }
            }
        }
    }

    schema, _, _, _, _, _, _ = main.build_input_schema([], request_body, document)

    parts = schema["properties"]["parts"]
    assert parts["type"] == "array"
    assert parts["items"] == {"type": "object"}
    assert parts["description"] == "New parts"
    assert parts["default"] == []


def test_recursive_ref_is_truncated_instead_of_hanging():
    """No Nextcloud schema is self-referential today, but one appearing later must
    truncate rather than recurse forever while building the tool list."""
    document = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Node"},
                        },
                    },
                }
            }
        }
    }
    request_body = {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}
    }

    schema, body_mode, _, _, _, _, _ = main.build_input_schema([], request_body, document)

    assert body_mode == "flattened_json_object"
    assert "$ref" not in json_of(schema)
    assert schema["properties"]["label"] == {"type": "string"}
    # The cycle is cut the second time Node is reached, leaving a permissive
    # object rather than another expansion.
    assert schema["properties"]["children"]["items"] == {
        "type": "object",
        "description": "Unexpanded schema (Node)",
    }


def test_unresolvable_ref_degrades_to_a_generic_object():
    """A pointer we cannot follow must not crash discovery nor leak the pointer;
    the argument stays usable as a free-form object."""
    parameters = [
        {"name": "filter", "in": "query", "schema": {"$ref": "#/components/schemas/Nope"}},
    ]

    schema, _, _, _, _, query_params, _ = main.build_input_schema(parameters, None, {"components": {}})

    assert query_params == ["filter"]
    assert schema["properties"]["filter"]["type"] == "object"
    assert "$ref" not in json_of(schema)


def test_refs_are_stripped_even_without_a_document():
    """build_input_schema is reachable without the spec root; the schema it
    returns still has to be self-contained."""
    request_body = {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Whatever"}}}
    }

    schema, body_mode, _, _, _, _, _ = main.build_input_schema([], request_body)

    assert body_mode == "body"
    assert "$ref" not in json_of(schema)


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


def run_mcp(coro_factory, operations=None, apps=None):
    """Each run gets a fresh app: a StreamableHTTPSessionManager may only be
    started once, so the module-level instance cannot be reused across tests."""

    async def runner():
        mcp_app = main.build_mcp_app()
        async with mcp_app.router.lifespan_context(mcp_app):
            # Overwrite the state the lifespan's startup discovery just produced,
            # so the assertions below do not depend on a reachable Nextcloud.
            main.DISCOVERY_STATE = make_state(
                operations=operations or {},
                apps=apps or [],
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
    assert [tool["name"] for tool in result["tools"]] == META_TOOL_NAMES


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


def test_tool_surface_stays_fixed_however_many_operations_are_discovered():
    """The point of the meta-tool surface: a 550-operation instance must not put
    550 schemas into every client's context before a single call is made."""
    operations = catalogue(*(make_operation(f"app_op_{index}") for index in range(400)))

    result = run_mcp(lambda client: post_mcp(client, "tools/list"), operations=operations)["result"]

    assert [tool["name"] for tool in result["tools"]] == META_TOOL_NAMES


def test_find_ranks_a_name_match_above_a_summary_only_match():
    """Searching `page create` has to surface the create operation itself, not
    every operation whose prose happens to mention creating pages."""
    operations = catalogue(
        make_operation("collectives_page_create", summary="Create a page"),
        make_operation("collectives_page_trash", summary="Trash a page you did not create"),
    )

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.FIND_TOOL_NAME, "arguments": {"query": "page create"}},
            call_headers(main.FIND_TOOL_NAME),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert [row["name"] for row in result["operations"]] == [
        "collectives_page_create",
        "collectives_page_trash",
    ]


def test_find_reports_truncation_so_the_caller_knows_to_narrow():
    operations = catalogue(*(make_operation(f"collectives_page_{index}") for index in range(50)))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.FIND_TOOL_NAME, "arguments": {"query": "page", "limit": 5}},
            call_headers(main.FIND_TOOL_NAME),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["total_matches"] == 50
    assert result["returned"] == 5
    assert result["truncated"] is True


def test_find_restricted_to_an_app_excludes_other_apps():
    operations = catalogue(
        make_operation("collectives_page_get", summary="Get a page"),
        make_operation("spreed_room_get", app_id="spreed", summary="Get a room page"),
    )

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.FIND_TOOL_NAME, "arguments": {"query": "get", "app": "collectives"}},
            call_headers(main.FIND_TOOL_NAME),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert [row["name"] for row in result["operations"]] == ["collectives_page_get"]


def test_describe_returns_the_schema_that_is_no_longer_in_the_tool_list():
    """The schema left tools/list, so describe is now the only way a caller can
    learn an operation's arguments. If it stops carrying them, calls go blind."""
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    operations = catalogue(make_operation("collectives_page_create", input_schema=schema))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {
                "name": main.DESCRIBE_TOOL_NAME,
                "arguments": {"names": ["collectives_page_create"]},
            },
            call_headers(main.DESCRIBE_TOOL_NAME),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is True
    assert result["operations"][0]["input_schema"] == schema


def test_describe_suggests_alternatives_for_an_unknown_name():
    """Without the tool list to autocomplete against, a near-miss name is the
    normal failure mode; it has to be recoverable without a second search."""
    operations = catalogue(make_operation("collectives_page_create", summary="Create a page"))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.DESCRIBE_TOOL_NAME, "arguments": {"names": ["collectives_create_page"]}},
            call_headers(main.DESCRIBE_TOOL_NAME),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is False
    # The built-in webdav entries are always in the catalogue, so they can be
    # suggested too; what matters is that the intended operation ranks first.
    assert result["unknown"][0]["did_you_mean"][0] == "collectives_page_create"


def test_call_rejects_an_unknown_operation_without_reaching_nextcloud():
    operations = catalogue(make_operation("collectives_page_create", summary="Create a page"))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {
                "name": main.CALL_TOOL_NAME,
                "arguments": {"name": "collectives_page_nope", "arguments": {}},
            },
            call_headers(main.CALL_TOOL_NAME),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is False
    assert result["did_you_mean"] == ["collectives_page_create"]


def test_call_without_credentials_still_refuses_after_the_meta_tool_rewrite():
    """Execution used to be gated per dynamic tool. Routing every call through
    one tool must not let an uncredentialed caller reach Nextcloud."""
    operations = catalogue(make_operation("collectives_page_create"))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {
                "name": main.CALL_TOOL_NAME,
                "arguments": {"name": "collectives_page_create", "arguments": {}},
            },
            call_headers(main.CALL_TOOL_NAME, authenticated=False),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is False
    assert "Missing request credentials" in result["error"]


# --- discovery is gated as tightly as execution ------------------------------
#
# Execution was credentialed from the start, but searching the catalogue was
# not. The catalogue names every installed app and every API path it exposes,
# so leaving it open let anyone who knew the endpoint URL enumerate the whole
# instance - reconnaissance, even though they could not then call anything.


def test_find_without_credentials_refuses():
    operations = catalogue(make_operation("collectives_page_create", summary="Create a page"))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.FIND_TOOL_NAME, "arguments": {"query": "page"}},
            call_headers(main.FIND_TOOL_NAME, authenticated=False),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is False
    assert "Missing request credentials" in result["error"]
    assert "operations" not in result


def test_describe_without_credentials_refuses():
    operations = catalogue(make_operation("collectives_page_create"))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.DESCRIBE_TOOL_NAME, "arguments": {"names": ["collectives_page_create"]}},
            call_headers(main.DESCRIBE_TOOL_NAME, authenticated=False),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is False
    assert "operations" not in result


def test_unknown_operation_without_credentials_does_not_leak_suggestions():
    """did_you_mean is built from the catalogue, so answering an unknown name
    before checking credentials would let an anonymous caller enumerate
    operations one guess at a time."""
    operations = catalogue(make_operation("collectives_page_create", summary="Create a page"))

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {
                "name": main.CALL_TOOL_NAME,
                "arguments": {"name": "collectives_page_nope", "arguments": {}},
            },
            call_headers(main.CALL_TOOL_NAME, authenticated=False),
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert result["ok"] is False
    assert "did_you_mean" not in result


def test_find_tool_description_hides_the_app_index_without_credentials():
    """The find tool's own description carries the app inventory, so it leaks
    through tools/list even when the tool itself refuses to run."""
    apps = [{"id": "collectives", "name": "Collectives", "operation_count": 84}]

    anonymous = run_mcp(lambda client: post_mcp(client, "tools/list"), apps=apps)["result"]
    credentialed = run_mcp(
        lambda client: post_mcp(client, "tools/list", headers=AUTH_HEADERS), apps=apps
    )["result"]

    def find_description(result):
        return next(t for t in result["tools"] if t["name"] == main.FIND_TOOL_NAME)["description"]

    assert "collectives:84" not in find_description(anonymous)
    assert "collectives:84" in find_description(credentialed)


def test_status_without_credentials_hides_the_instance():
    """GET / is deliberately trimmed of these fields; that is pointless if the
    status tool hands them to anyone who can reach /mcp."""
    apps = [{"id": "collectives", "name": "Collectives", "operation_count": 84}]

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.STATUS_TOOL_NAME, "arguments": {}},
            {"Mcp-Name": main.STATUS_TOOL_NAME},
        ),
        apps=apps,
    )["result"]["structuredContent"]

    assert "nextcloud_url" not in result
    assert "api_viewer_url" not in result
    assert "apps" not in result
    assert "last_error" not in result
    # The caller's own auth diagnostics stay open - that is how someone whose
    # credentials are not working finds out why.
    assert result["request_auth_configured"] is False
    assert result["app_count"] == 1


def test_status_with_credentials_reports_the_instance():
    apps = [{"id": "collectives", "name": "Collectives", "operation_count": 84}]

    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.STATUS_TOOL_NAME, "arguments": {}},
            call_headers(main.STATUS_TOOL_NAME),
        ),
        apps=apps,
    )["result"]["structuredContent"]

    assert result["nextcloud_url"] == main.NEXTCLOUD_URL
    assert [app["id"] for app in result["apps"]] == ["collectives"]


def test_status_refresh_requires_credentials():
    """A forced refresh re-fetches every app's OpenAPI document using the
    server's own credentials, so anonymous access turns one request into ~29
    upstream ones."""
    operations = catalogue(make_operation("collectives_page_create"))

    payload = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.STATUS_TOOL_NAME, "arguments": {"refresh": True}},
            {"Mcp-Name": main.STATUS_TOOL_NAME},
        ),
        operations=operations,
    )["result"]["structuredContent"]

    assert payload["ok"] is False
    assert "Missing request credentials" in payload["error"]
    # Discovery would have failed against the unreachable test URL and wiped the
    # catalogue; it surviving proves the refresh never ran.
    assert len(main.DISCOVERY_STATE.operations) == 1


# --- WebDAV file access ------------------------------------------------------
#
# ocs_api_viewer describes the OCS API only, so no discovered operation can
# return a file's bytes - collectives_page_get reports a page's size but never
# its body. These two tools are hand-written to cover that gap.


def webdav_auth(username="alice"):
    return main.AuthContext(
        auth_header="Basic YWxpY2U6dA==", source="request_basic_headers",
        cache_key="request:x", username=username,
    )


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = httpx2.Headers(headers or {})
        self.content = body

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        import json

        return json.loads(self.text)


class FakeClient:
    """Records every call. `responses` may be one response or a queue of them,
    because create_folder walks a chain and delete probes before acting."""

    def __init__(self, responses, calls):
        self._responses = list(responses) if isinstance(responses, list) else [responses]
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def _next(self, entry):
        self._calls.append(entry)
        return self._responses[min(len(self._calls) - 1, len(self._responses) - 1)]

    async def get(self, url, headers=None):
        return self._next({"method": "GET", "url": url, "headers": headers})

    async def put(self, url, headers=None, content=None):
        return self._next({"method": "PUT", "url": url, "headers": headers, "content": content})

    async def request(self, method, url, headers=None, content=None):
        return self._next({"method": method, "url": url, "headers": headers, "content": content})


def run_webdav(coro_factory, responses):
    """Swap httpx2.AsyncClient for a recorder so the WebDAV request that would
    have gone out can be asserted on. Returns (result, first call) for the common
    single-request case; `calls` on the record holds the full sequence."""
    calls = []
    original = main.httpx2.AsyncClient
    main.httpx2.AsyncClient = lambda **_: FakeClient(responses, calls)
    try:
        result = asyncio.run(coro_factory())
    finally:
        main.httpx2.AsyncClient = original
    record = dict(calls[0]) if calls else {}
    record["calls"] = calls
    return result, record


MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/alice/kb/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/kb/%E7%AC%AC%E4%B8%80%E7%AB%A0%20%E6%A6%82%E8%AB%96/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/kb/notes.md</d:href>
    <d:propstat><d:prop>
      <d:resourcetype/>
      <d:getcontentlength>143</d:getcontentlength>
      <d:getcontenttype>text/markdown</d:getcontenttype>
      <d:getetag>"abc-gzip"</d:getetag>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


def test_header_auth_context_keeps_the_username_for_webdav():
    """The username arrives on every request but used to be dropped once it had
    been folded into the Basic header; WebDAV paths need it back."""
    ctx = type("Ctx", (), {"request": FakeRequest(
        {"x-nextcloud-username": "alice", "x-nextcloud-apptoken": "alice-token"}
    )})()

    assert main.execution_auth_context(ctx).username == "alice"


def test_webdav_url_encodes_spaces_and_non_ascii_per_segment():
    """Real paths here carry CJK names and spaces; separators must
    stay literal while the names around them are encoded."""
    url = main.webdav_url(webdav_auth(), ".Collectives/知識庫/第一章 概論/notes.md")

    assert url.startswith(f"{main.NEXTCLOUD_URL}/remote.php/dav/files/alice/")
    assert url.endswith("/.Collectives/%E7%9F%A5%E8%AD%98%E5%BA%AB/%E7%AC%AC%E4%B8%80%E7%AB%A0%20%E6%A6%82%E8%AB%96/notes.md")


def test_webdav_url_rejects_traversal():
    """Basic auth already confines the caller to their own files, but `..` could
    still climb out of the files root and address other DAV endpoints."""
    for path in ("../evil", ".Collectives/../../x", "a/./b"):
        try:
            main.webdav_url(webdav_auth(), path)
        except ValueError:
            continue
        raise AssertionError(f"traversal not rejected: {path}")


def test_webdav_url_requires_a_username():
    try:
        main.webdav_url(main.AuthContext(auth_header="Basic x", source="s", cache_key="c"), "a.md")
    except ValueError as exc:
        assert "username" in str(exc)
    else:
        raise AssertionError("missing username was not rejected")


def test_read_file_returns_content_and_etag():
    response = FakeResponse(
        headers={"content-type": "text/markdown; charset=utf-8", "etag": '"abc123"'},
        body="# 標題\n內文".encode("utf-8"),
    )

    payload, record = run_webdav(
        lambda: main.read_file(webdav_auth(), ".Collectives/kb/page.md"), response
    )

    assert record["method"] == "GET"
    assert payload["ok"] is True
    assert payload["data"] == "# 標題\n內文"
    assert payload["etag"] == '"abc123"'


def test_read_file_base64_encodes_binary():
    response = FakeResponse(headers={"content-type": "image/png"}, body=b"\x89PNG\r\n")

    payload, _ = run_webdav(lambda: main.read_file(webdav_auth(), "photo.png"), response)

    assert payload["data_base64"] == "iVBORw0K"
    assert "data" not in payload


def test_etag_from_a_compressed_read_is_usable_as_if_match():
    """Observed against a live instance: a GET that the server gzips comes back
    with `"<hash>-gzip"`, but the entity's validator is `"<hash>"`. Returning the
    mangled value made every guarded write fail with 412 while nothing had
    changed, and re-reading returned the same mangled value - an unbreakable
    loop, with an error message that blamed a concurrent edit."""
    write_etag = '"98b5f8bc2afdd50bf5b24f4a39d4c2d1"'
    read_response = FakeResponse(
        headers={"content-type": "text/markdown", "etag": '"98b5f8bc2afdd50bf5b24f4a39d4c2d1-gzip"'},
        body=b"x",
    )

    payload, _ = run_webdav(lambda: main.read_file(webdav_auth(), "kb/page.md"), read_response)

    assert payload["etag"] == write_etag


def test_etag_normalisation_covers_the_other_encodings_and_weak_validators():
    # -zstd was also observed live; W/ is the standard weak-validator prefix.
    assert main.normalize_etag('"abc-zstd"') == '"abc"'
    assert main.normalize_etag('"abc-br"') == '"abc"'
    assert main.normalize_etag('"abc-deflate"') == '"abc"'
    assert main.normalize_etag('W/"abc-gzip"') == 'W/"abc"'
    # Untouched: no suffix, and a hash that merely ends in something similar.
    assert main.normalize_etag('"abc"') == '"abc"'
    assert main.normalize_etag(None) is None


def test_a_stale_if_match_from_before_the_fix_is_normalised_too():
    """Callers may still be holding a mangled ETag from an earlier read, so the
    inbound value is normalised as well rather than only the outbound one."""
    response = FakeResponse(status_code=204, headers={"etag": '"new"'})

    _, record = run_webdav(
        lambda: main.write_file(webdav_auth(), "kb/page.md", "x", if_match='"abc-gzip"'),
        response,
    )

    assert record["headers"]["If-Match"] == '"abc"'


def test_write_file_sends_utf8_bytes_and_if_match():
    response = FakeResponse(status_code=204, headers={"etag": '"new"'})

    payload, record = run_webdav(
        lambda: main.write_file(webdav_auth(), "kb/page.md", "新內容", if_match='"old"'),
        response,
    )

    assert record["method"] == "PUT"
    assert record["content"] == "新內容".encode("utf-8")
    assert record["headers"]["If-Match"] == '"old"'
    assert payload["ok"] is True and payload["etag"] == '"new"'


def test_write_file_omits_if_match_when_not_given():
    response = FakeResponse(status_code=201)

    _, record = run_webdav(lambda: main.write_file(webdav_auth(), "kb/page.md", "x"), response)

    assert "If-Match" not in record["headers"]


def test_write_file_explains_a_precondition_failure():
    """A bare 412 tells the caller nothing about how to recover, and the wrong
    recovery here is re-writing stale content over somebody's edit."""
    response = FakeResponse(status_code=412)

    payload, _ = run_webdav(
        lambda: main.write_file(webdav_auth(), "kb/page.md", "x", if_match='"stale"'), response
    )

    assert payload["ok"] is False
    assert "Read it again" in payload["error"]


# --- gaps found by testing against a real instance ---------------------------
#
# Everything below was written after live testing contradicted an assumption the
# code or the docs had made.


def test_a_missing_parent_reports_404_not_the_409_the_spec_suggests():
    """Nextcloud answers a write below a missing folder with 404. The code only
    mapped 409, so the guidance never fired and callers got raw SabreDAV XML."""
    response = FakeResponse(status_code=404, body=b"<d:error>not found</d:error>")

    payload, _ = run_webdav(
        lambda: main.write_file(webdav_auth(), "nope/deeper/page.md", "x"), response
    )

    assert payload["ok"] is False
    assert "webdav_create_folder" in payload["error"]


def test_reading_a_folder_is_an_error_not_a_false_success():
    """Live, `read_file("Documents")` returned 200 and the HTML blurb Nextcloud
    serves for a collection, so a mistyped path looked like a file whose contents
    were 'This is the WebDAV interface...'."""
    response = FakeResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=UTF-8"},
        body=b"This is the WebDAV interface. It can only be accessed by WebDAV clients",
    )

    payload, _ = run_webdav(lambda: main.read_file(webdav_auth(), "Documents"), response)

    assert payload["ok"] is False
    assert "webdav_list_directory" in payload["error"]
    assert "data" not in payload


def test_a_real_file_is_not_mistaken_for_a_folder():
    """The collection check keys on a missing ETag, so an ordinary HTML file -
    which does have one - must still read normally."""
    response = FakeResponse(
        status_code=200,
        headers={"content-type": "text/html", "etag": '"abc"'},
        body=b"<h1>a real page</h1>",
    )

    payload, _ = run_webdav(lambda: main.read_file(webdav_auth(), "page.html"), response)

    assert payload["ok"] is True
    assert payload["data"] == "<h1>a real page</h1>"


def test_binary_round_trip_uses_content_base64():
    """Reads hand binary back base64-encoded. With only a text field, feeding
    that back wrote the base64 itself and the next read encoded it again -
    silent corruption that reported success at every step."""
    original = b"\x89PNG\r\n\x1a\n\x00\xff"
    response = FakeResponse(status_code=204, headers={"etag": '"new"'})

    payload, record = run_webdav(
        lambda: main.write_file(
            webdav_auth(), "photo.png", content_base64=base64.b64encode(original).decode()
        ),
        response,
    )

    assert record["content"] == original
    assert payload["bytes_written"] == len(original)


def test_content_and_content_base64_are_mutually_exclusive():
    try:
        main.write_payload_bytes("text", base64.b64encode(b"bytes").decode())
    except ValueError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("passing both was not rejected")


def test_invalid_base64_is_rejected_before_the_request():
    try:
        main.write_payload_bytes(None, "not valid base64!!")
    except ValueError as exc:
        assert "base64" in str(exc)
    else:
        raise AssertionError("invalid base64 was not rejected")


def test_propfind_entries_come_back_as_reusable_relative_paths():
    """A listing is only useful if its paths can be handed straight to the other
    file tools, so the DAV prefix and percent-encoding have to come off."""
    entries = main.parse_propfind(MULTISTATUS, webdav_auth())

    assert [e["path"] for e in entries] == ["kb", "kb/第一章 概論", "kb/notes.md"]
    assert entries[1]["is_folder"] is True
    assert entries[2]["is_folder"] is False
    assert entries[2]["size"] == 143
    # Listings carry ETags too, and they get mangled by compression just the same.
    assert entries[2]["etag"] == '"abc"'


def test_list_directory_drops_the_folder_itself():
    """PROPFIND Depth 1 returns the folder first; the caller asked what is inside."""
    response = FakeResponse(status_code=207, body=MULTISTATUS.encode("utf-8"))

    payload, record = run_webdav(lambda: main.list_directory(webdav_auth(), "kb"), response)

    assert record["method"] == "PROPFIND"
    assert record["headers"]["Depth"] == "1"
    assert [e["name"] for e in payload["entries"]] == ["第一章 概論", "notes.md"]


def test_list_directory_accepts_the_files_root():
    """Every other path is required; the root is the one legitimate empty path."""
    response = FakeResponse(status_code=207, body=MULTISTATUS.encode("utf-8"))

    _, record = run_webdav(lambda: main.list_directory(webdav_auth(), ""), response)

    assert record["url"].endswith("/remote.php/dav/files/alice")


def test_create_folder_walks_the_whole_chain():
    """MKCOL makes exactly one level, so uploading a tree is impossible unless
    the missing intermediates are created first."""
    response = FakeResponse(status_code=201)

    payload, record = run_webdav(
        lambda: main.create_folder(webdav_auth(), "a/b/c"), response
    )

    assert [c["method"] for c in record["calls"]] == ["MKCOL", "MKCOL", "MKCOL"]
    assert payload["created"] == ["a", "a/b", "a/b/c"]


def test_create_folder_treats_an_existing_folder_as_success():
    """405 means it is already there. A caller looping over a tree should not
    have to tell 'made it' apart from 'already present'."""
    response = FakeResponse(status_code=405)

    payload, _ = run_webdav(lambda: main.create_folder(webdav_auth(), "a/b"), response)

    assert payload["ok"] is True
    assert payload["created"] == []
    assert payload["already_existed"] == ["a", "a/b"]


def test_create_folder_without_parents_only_makes_the_leaf():
    response = FakeResponse(status_code=201)

    payload, record = run_webdav(
        lambda: main.create_folder(webdav_auth(), "a/b/c", parents=False), response
    )

    assert len(record["calls"]) == 1
    assert payload["created"] == ["a/b/c"]


def test_delete_refuses_a_folder_unless_recursive_is_explicit():
    """WebDAV DELETE on a collection always takes everything inside, and offers
    no shallow variant, so a mistyped path could remove a subtree."""
    probe = FakeResponse(status_code=207, body=MULTISTATUS.encode("utf-8"))

    payload, record = run_webdav(lambda: main.delete_path(webdav_auth(), "kb"), probe)

    assert payload["ok"] is False
    assert payload["is_folder"] is True
    assert [c["method"] for c in record["calls"]] == ["PROPFIND"]  # never reached DELETE


def test_delete_removes_a_folder_when_recursive_is_given():
    probe = FakeResponse(status_code=207, body=MULTISTATUS.encode("utf-8"))
    deleted = FakeResponse(status_code=204)

    payload, record = run_webdav(
        lambda: main.delete_path(webdav_auth(), "kb", recursive=True), [probe, deleted]
    )

    assert payload["ok"] is True
    assert [c["method"] for c in record["calls"]] == ["PROPFIND", "DELETE"]


def test_delete_removes_a_file_without_needing_recursive():
    file_probe = FakeResponse(
        status_code=207,
        body="""<?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:"><d:response>
          <d:href>/remote.php/dav/files/alice/notes.md</d:href>
          <d:propstat><d:prop><d:resourcetype/></d:prop></d:propstat>
        </d:response></d:multistatus>""".encode("utf-8"),
    )
    deleted = FakeResponse(status_code=204)

    payload, _ = run_webdav(
        lambda: main.delete_path(webdav_auth(), "notes.md"), [file_probe, deleted]
    )

    assert payload["ok"] is True
    assert payload["is_folder"] is False


def test_webdav_operations_are_in_the_catalogue_not_the_tool_list():
    """They are published through find/describe/call like everything else, so the
    fixed surface stays small and each operation's warnings arrive from describe
    at the moment of use rather than sitting in context all session."""
    listed = run_mcp(lambda client: post_mcp(client, "tools/list"))["result"]
    assert [t["name"] for t in listed["tools"]] == META_TOOL_NAMES

    found = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {"name": main.FIND_TOOL_NAME, "arguments": {"app": main.WEBDAV_APP_ID}},
            call_headers(main.FIND_TOOL_NAME),
        )
    )["result"]["structuredContent"]

    assert sorted(row["name"] for row in found["operations"]) == [
        "webdav_create_folder", "webdav_delete", "webdav_list_directory",
        "webdav_read_file", "webdav_write_file",
    ]


def test_a_webdav_operation_still_refuses_an_uncredentialed_caller():
    """Routing them through call_operation must not lose the credential gate."""
    result = run_mcp(
        lambda client: post_mcp(
            client,
            "tools/call",
            {
                "name": main.CALL_TOOL_NAME,
                "arguments": {"name": "webdav_read_file", "arguments": {"path": "kb/page.md"}},
            },
            call_headers(main.CALL_TOOL_NAME, authenticated=False),
        )
    )["result"]["structuredContent"]

    assert result["ok"] is False
    assert "Missing request credentials" in result["error"]


def test_the_find_description_advertises_the_builtin_webdav_app():
    """Nothing discovers these, so without the index entry a client would have no
    way to learn they exist."""
    listed = run_mcp(
        lambda client: post_mcp(client, "tools/list", headers=AUTH_HEADERS)
    )["result"]
    find = next(t for t in listed["tools"] if t["name"] == main.FIND_TOOL_NAME)

    assert f"{main.WEBDAV_APP_ID}:{len(main.BUILTIN_OPERATIONS)}" in find["description"]


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
