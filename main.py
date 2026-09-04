import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx2
import mcp.server.stdio
import uvicorn
from mcp import types
from mcp.server import CacheHint, Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("nextcloud-mcp")


NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL", "http://nc31-app-1:80").rstrip("/")
NEXTCLOUD_USERNAME = os.getenv("NEXTCLOUD_USERNAME")
NEXTCLOUD_APP_TOKEN = os.getenv("NEXTCLOUD_APP_TOKEN")
API_VIEWER_URL = f"{NEXTCLOUD_URL}/apps/ocs_api_viewer"

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "streamable-http")
DISCOVERY_TIMEOUT_SECONDS = float(os.getenv("DISCOVERY_TIMEOUT_SECONDS", "30"))
DISCOVERY_RETRY_SECONDS = float(os.getenv("DISCOVERY_RETRY_SECONDS", "60"))
TOOL_LIST_TTL_MS = int(os.getenv("TOOL_LIST_TTL_MS", "300000"))
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
MAX_REF_DEPTH = 8
STATUS_TOOL_NAME = "nextcloud_discovery_status"
FIND_TOOL_NAME = "nextcloud_find_operations"
DESCRIBE_TOOL_NAME = "nextcloud_describe_operations"
CALL_TOOL_NAME = "nextcloud_call_operation"
FIND_DEFAULT_LIMIT = 30
FIND_MAX_LIMIT = 200
SUGGESTION_LIMIT = 5
INTERNAL_HEADER_PARAMS = {"ocs-apirequest", "authorization"}
SERVER_NAME = "nextcloud-live-instance-mcp"
SERVER_VERSION = "2.0.0"
NEXTCLOUD_USERNAME_HEADER = "x-nextcloud-username"
NEXTCLOUD_APP_TOKEN_HEADER = "x-nextcloud-apptoken"
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "etag",
    "last-modified",
    "location",
    "retry-after",
}


@dataclass(slots=True)
class OperationDefinition:
    name: str
    app_id: str
    app_name: str
    method: str
    path: str
    summary: str
    description: str
    input_schema: dict[str, Any]
    path_params: list[str]
    query_params: list[str]
    header_params: list[str]
    body_mode: str
    body_fields: list[str]
    body_content_type: str | None


@dataclass(slots=True)
class DiscoveryState:
    operations: dict[str, OperationDefinition] = field(default_factory=dict)
    apps: list[dict[str, Any]] = field(default_factory=list)
    last_refresh: str | None = None
    last_error: str | None = None
    last_attempt: float | None = None


@dataclass(slots=True)
class AuthContext:
    auth_header: str | None
    source: str
    cache_key: str


DISCOVERY_STATE = DiscoveryState()
DISCOVERY_LOCK = asyncio.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unique_names(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_tool_name(app_id: str, operation_id: str | None, method: str, path: str) -> str:
    raw = operation_id or f"{method}_{path}"
    normalized = "".join(ch if ch.isalnum() else "_" for ch in f"{app_id}_{raw}")
    normalized = "_".join(part for part in normalized.lower().split("_") if part)
    return normalized or f"{app_id}_{method}"


def clone_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    return copy.deepcopy(schema or {})


def enrich_schema(schema: dict[str, Any], description: str | None) -> dict[str, Any]:
    enriched = clone_schema(schema)
    if description and "description" not in enriched:
        enriched["description"] = description
    return enriched


def resolve_json_pointer(document: dict[str, Any] | None, pointer: str) -> Any:
    if not isinstance(document, dict) or not pointer.startswith("#/"):
        return None

    node: Any = document
    for token in pointer[2:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if key not in node:
                return None
            node = node[key]
        elif isinstance(node, list):
            try:
                node = node[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def truncated_ref_schema(pointer: str) -> dict[str, Any]:
    # No additionalProperties: False here - the real shape is unknown, so the
    # placeholder has to keep accepting whatever the caller sends.
    return {"type": "object", "description": f"Unexpanded schema ({pointer.rsplit('/', 1)[-1]})"}


def resolve_schema_refs(
    schema: Any,
    document: dict[str, Any] | None,
    ref_stack: tuple[str, ...] = (),
    depth: int = 0,
) -> Any:
    """Inline every `$ref` so the published inputSchema stands on its own.

    MCP clients only ever see a tool's inputSchema, never the OpenAPI document it
    came from, so a surviving `#/components/schemas/...` pointer is unresolvable
    on their side and can take out the whole tool list.
    """
    if isinstance(schema, list):
        return [resolve_schema_refs(item, document, ref_stack, depth) for item in schema]

    if not isinstance(schema, dict):
        return copy.deepcopy(schema)

    pointer = schema.get("$ref")
    if not isinstance(pointer, str):
        return {key: resolve_schema_refs(value, document, ref_stack, depth) for key, value in schema.items()}

    siblings = {
        key: resolve_schema_refs(value, document, ref_stack, depth)
        for key, value in schema.items()
        if key != "$ref"
    }

    if pointer in ref_stack or depth >= MAX_REF_DEPTH:
        return {**truncated_ref_schema(pointer), **siblings}

    target = resolve_json_pointer(document, pointer)
    if not isinstance(target, dict):
        logger.warning("Unresolvable schema reference %s, falling back to a generic object", pointer)
        return {**truncated_ref_schema(pointer), **siblings}

    resolved = resolve_schema_refs(target, document, ref_stack + (pointer,), depth + 1)
    return {**resolved, **siblings}


def merge_parameters(
    path_parameters: list[dict[str, Any]],
    operation_parameters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for parameter in path_parameters + operation_parameters:
        location = parameter.get("in")
        name = parameter.get("name")
        if not location or not name:
            continue
        merged[(location, name)] = parameter
    return list(merged.values())


def preferred_content_type(content: dict[str, Any]) -> str | None:
    if not content:
        return None
    candidates = [
        "application/json",
        "application/merge-patch+json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ]
    for candidate in candidates:
        if candidate in content:
            return candidate
    for content_type in content:
        if "json" in content_type:
            return content_type
    return next(iter(content), None)


def build_input_schema(
    parameters: list[dict[str, Any]],
    request_body: dict[str, Any] | None,
    document: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, list[str], str | None, list[str], list[str], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    header_params: list[str] = []

    for parameter in parameters:
        name = parameter["name"]
        location = parameter["in"]
        if location == "header" and name.lower() in INTERNAL_HEADER_PARAMS:
            continue
        resolved = resolve_schema_refs(parameter.get("schema", {}), document)
        schema = enrich_schema(resolved, parameter.get("description"))
        properties[name] = schema
        if parameter.get("required"):
            required.append(name)
        if location == "path":
            path_params.append(name)
        elif location == "query":
            query_params.append(name)
        elif location == "header":
            header_params.append(name)

    body_mode = "none"
    body_fields: list[str] = []
    body_content_type: str | None = None

    if request_body:
        content = request_body.get("content", {})
        body_content_type = preferred_content_type(content)
        # Resolved before the flatten check below, otherwise a body that is itself
        # a single `$ref` would look like a non-object and never get flattened.
        body_schema = resolve_schema_refs(
            clone_schema(content.get(body_content_type, {}).get("schema", {})),
            document,
        )

        can_flatten = (
            body_content_type is not None
            and "json" in body_content_type
            and body_schema.get("type") == "object"
            and isinstance(body_schema.get("properties"), dict)
            and not body_schema.get("additionalProperties")
        )

        body_property_names = list(body_schema.get("properties", {}).keys())
        has_conflicts = any(name in properties for name in body_property_names)

        if can_flatten and body_property_names and not has_conflicts:
            body_mode = "flattened_json_object"
            for field_name, field_schema in body_schema.get("properties", {}).items():
                properties[field_name] = field_schema
            body_fields = body_property_names
            required.extend(body_schema.get("required", []))
        else:
            body_mode = "body"
            body_fields = ["body"]
            body_property = enrich_schema(body_schema or {"type": "object"}, request_body.get("description"))
            properties["body"] = body_property
            if request_body.get("required"):
                required.append("body")

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": unique_names(required),
        "additionalProperties": False,
    }
    # Last line of defence: whatever the document did or did not contain, no `$ref`
    # may reach the client. Passing document=None degrades any survivor to an object.
    input_schema = resolve_schema_refs(input_schema, None)
    return (
        input_schema,
        body_mode,
        body_fields,
        body_content_type,
        path_params,
        query_params,
        header_params,
    )


def make_operation_definition(
    app_meta: dict[str, Any],
    path: str,
    method: str,
    operation: dict[str, Any],
    inherited_parameters: list[dict[str, Any]],
    document: dict[str, Any] | None = None,
) -> OperationDefinition:
    parameters = merge_parameters(inherited_parameters, operation.get("parameters", []))
    (
        input_schema,
        body_mode,
        body_fields,
        body_content_type,
        path_params,
        query_params,
        header_params,
    ) = build_input_schema(parameters, operation.get("requestBody"), document)

    summary = operation.get("summary") or f"{method.upper()} {path}"
    description = operation.get("description") or summary

    return OperationDefinition(
        name=normalize_tool_name(app_meta["id"], operation.get("operationId"), method, path),
        app_id=app_meta["id"],
        app_name=app_meta.get("name", app_meta["id"]),
        method=method.upper(),
        path=path,
        summary=summary,
        description=description,
        input_schema=input_schema,
        path_params=path_params,
        query_params=query_params,
        header_params=header_params,
        body_mode=body_mode,
        body_fields=body_fields,
        body_content_type=body_content_type,
    )


def encode_basic_auth(username: str, app_token: str) -> str:
    token = base64.b64encode(f"{username}:{app_token}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def hash_auth_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def default_auth_context() -> AuthContext:
    if NEXTCLOUD_USERNAME and NEXTCLOUD_APP_TOKEN:
        auth_header = encode_basic_auth(NEXTCLOUD_USERNAME, NEXTCLOUD_APP_TOKEN)
        return AuthContext(
            auth_header=auth_header,
            source="env_basic",
            cache_key=f"env:{hash_auth_value(auth_header)}",
        )
    return AuthContext(auth_header=None, source="none", cache_key="anonymous")


def header_auth_context(headers: Any) -> AuthContext:
    username = headers.get(NEXTCLOUD_USERNAME_HEADER)
    app_token = headers.get(NEXTCLOUD_APP_TOKEN_HEADER)
    if username and app_token:
        auth_header = encode_basic_auth(username, app_token)
        return AuthContext(
            auth_header=auth_header,
            source="request_basic_headers",
            cache_key=f"request:{hash_auth_value(auth_header)}",
        )
    return AuthContext(auth_header=None, source="none", cache_key="anonymous")


def execution_auth_context(ctx: ServerRequestContext[Any, Any]) -> AuthContext:
    """Credentials used to proxy a tool call.

    On HTTP the server is shared, so only the caller's own request headers are
    trusted and there is no fallback to the startup account. On stdio the
    process belongs to a single local client and carries no headers at all, so
    the configured environment credentials are the caller's own.
    """
    if ctx.request is None:
        return default_auth_context()
    return header_auth_context(ctx.request.headers)


def build_nextcloud_headers(
    auth_context: AuthContext,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, */*",
        "OCS-APIRequest": "true",
    }
    if auth_context.auth_header:
        headers["Authorization"] = auth_context.auth_header
    if extra_headers:
        headers.update(extra_headers)
    return headers


def safe_response_headers(headers: Any) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in SAFE_RESPONSE_HEADERS
    }


async def fetch_json(client: httpx2.AsyncClient, url: str, auth_context: AuthContext) -> Any:
    response = await client.get(url, headers=build_nextcloud_headers(auth_context))
    response.raise_for_status()
    return response.json()


async def discover_operations(auth_context: AuthContext) -> DiscoveryState:
    if not auth_context.auth_header:
        raise RuntimeError(
            "Missing server discovery credentials. Set `NEXTCLOUD_USERNAME` "
            "and `NEXTCLOUD_APP_TOKEN` in the MCP server environment."
        )

    logger.info("Discovering Nextcloud APIs from %s", API_VIEWER_URL)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        apps = await fetch_json(client, f"{API_VIEWER_URL}/apps", auth_context)
        operations: dict[str, OperationDefinition] = {}
        discovered_apps: list[dict[str, Any]] = []

        for app_meta in apps:
            app_id = app_meta.get("id")
            if not app_id:
                continue

            try:
                openapi = await fetch_json(client, f"{API_VIEWER_URL}/apps/{app_id}", auth_context)
            except Exception as exc:
                logger.warning("Skipping app %s: %s", app_id, exc)
                continue

            paths = openapi.get("paths", {})
            if not isinstance(paths, dict) or not paths:
                logger.info("Skipping app %s without usable OpenAPI paths", app_id)
                continue

            app_operation_count = 0
            for path, path_item in paths.items():
                if not isinstance(path_item, dict):
                    continue

                inherited_parameters = path_item.get("parameters", [])
                for method, operation in path_item.items():
                    if method not in HTTP_METHODS or not isinstance(operation, dict):
                        continue

                    definition = make_operation_definition(
                        app_meta=app_meta,
                        path=path,
                        method=method,
                        operation=operation,
                        inherited_parameters=inherited_parameters,
                        document=openapi,
                    )
                    operations[definition.name] = definition
                    app_operation_count += 1

            discovered_apps.append(
                {
                    "id": app_id,
                    "name": app_meta.get("name", app_id),
                    "operation_count": app_operation_count,
                }
            )

    state = DiscoveryState(
        operations=operations,
        apps=sorted(discovered_apps, key=lambda item: item["id"]),
        last_refresh=now_iso(),
        last_error=None,
        last_attempt=time.monotonic(),
    )
    logger.info(
        "Discovered %d apps and %d MCP tools",
        len(state.apps),
        len(state.operations),
    )
    return state


def current_state() -> DiscoveryState:
    return DISCOVERY_STATE


def discovery_is_current() -> bool:
    state = DISCOVERY_STATE
    if state.last_refresh is not None:
        return True
    if state.last_error is None or state.last_attempt is None:
        return False
    return (time.monotonic() - state.last_attempt) < DISCOVERY_RETRY_SECONDS


async def refresh_state(force: bool = False) -> DiscoveryState:
    global DISCOVERY_STATE

    if not force and discovery_is_current():
        return DISCOVERY_STATE

    async with DISCOVERY_LOCK:
        if not force and discovery_is_current():
            return DISCOVERY_STATE

        try:
            state = await discover_operations(default_auth_context())
        except Exception as exc:
            state = DiscoveryState(last_error=str(exc), last_attempt=time.monotonic())
            logger.warning("Discovery failed for auth source %s: %s", default_auth_context().source, exc)
        DISCOVERY_STATE = state
        return DISCOVERY_STATE


def discovery_status_payload(request_context: AuthContext) -> dict[str, Any]:
    discovery_auth_context = default_auth_context()
    state = current_state()
    return {
        "nextcloud_url": NEXTCLOUD_URL,
        "api_viewer_url": API_VIEWER_URL,
        "discovery_auth_source": discovery_auth_context.source,
        "discovery_auth_configured": discovery_auth_context.auth_header is not None,
        "request_auth_source": request_context.source,
        "request_auth_configured": request_context.auth_header is not None,
        "supported_request_headers": [
            "X-Nextcloud-Username",
            "X-Nextcloud-AppToken",
        ],
        "app_count": len(state.apps),
        "tool_count": len(state.operations),
        "apps": state.apps,
        "last_refresh": state.last_refresh,
        "last_error": state.last_error,
        "discovery_retry_seconds": DISCOVERY_RETRY_SECONDS,
    }


def operation_row(definition: OperationDefinition) -> dict[str, Any]:
    return {
        "name": definition.name,
        "method": definition.method,
        "path": definition.path,
        "summary": definition.summary,
    }


def operation_haystack(definition: OperationDefinition) -> str:
    return f"{definition.name} {definition.summary} {definition.path} {definition.app_name}".lower()


def term_score(definition: OperationDefinition, terms: list[str]) -> int:
    """A term hitting the operation name counts double, which keeps
    `collectives_page_create` ahead of operations that merely mention pages in
    their summary."""
    name = definition.name.lower()
    haystack = operation_haystack(definition)
    return sum(2 if term in name else 1 if term in haystack else 0 for term in terms)


def search_operations(
    query: str | None,
    app: str | None,
    limit: int,
) -> tuple[list[OperationDefinition], int]:
    """Rank discovered operations against a free-text query.

    Every term has to appear somewhere in an operation's searchable text, so
    adding a term always narrows the result.
    """
    terms = (query or "").lower().split()
    app_filter = (app or "").strip().lower()

    scored: list[tuple[int, str, OperationDefinition]] = []
    for definition in current_state().operations.values():
        if app_filter and definition.app_id.lower() != app_filter:
            continue
        if not all(term in operation_haystack(definition) for term in terms):
            continue
        scored.append((-term_score(definition, terms), definition.name, definition))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[:limit]], len(scored)


def suggest_operation_names(name: Any) -> list[str]:
    """Recover from a near-miss operation name.

    Deliberately not `search_operations`: the whole premise of a did-you-mean is
    that one of the caller's terms is wrong, so requiring every term to match -
    which is what makes the search itself useful - would return nothing exactly
    when a suggestion is needed. Any term may match here, best overlap first.
    """
    terms = str(name or "").replace("_", " ").lower().split()
    if not terms:
        return []

    scored: list[tuple[int, str]] = []
    for definition in current_state().operations.values():
        score = term_score(definition, terms)
        if score:
            scored.append((-score, definition.name))

    scored.sort()
    return [item[1] for item in scored[:SUGGESTION_LIMIT]]


def app_index() -> str:
    apps = current_state().apps
    if not apps:
        return f"none discovered yet - call {STATUS_TOOL_NAME}"
    return ", ".join(f"{app['id']}:{app['operation_count']}" for app in apps)


def list_tools() -> list[types.Tool]:
    """Publish a fixed four-tool surface instead of one tool per operation.

    An instance with every app enabled discovers ~550 operations. Publishing
    those as ~550 tools puts roughly 100k tokens of schema into every client's
    context before a single call is made, and no session uses more than a
    handful of them. The catalogue is searched on demand instead, which costs
    one extra round trip and about 1k tokens of context.
    """
    operation_count = len(current_state().operations)

    return [
        types.Tool(
            name=STATUS_TOOL_NAME,
            description=(
                "Show the live connected Nextcloud instance, discovery status, "
                "discovered apps, and available MCP tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": "Re-run API discovery before reporting status.",
                    }
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name=FIND_TOOL_NAME,
            description=(
                f"Search the {operation_count} live Nextcloud API operations available on the "
                f"connected instance and return their names, HTTP method, path and summary. "
                f"Start here, then call `{DESCRIBE_TOOL_NAME}` for the arguments of the "
                f"operations you picked, then `{CALL_TOOL_NAME}` to run one. "
                f"Use this only for real operations against the configured Nextcloud server, "
                f"not for documentation lookup or local workspace tasks. "
                f"Apps on this instance (id:operations) - {app_index()}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Space-separated terms matched against operation name, summary, "
                            "path and app name. Every term must match, so more terms narrow "
                            "the result. Omit to list an app in full."
                        ),
                    },
                    "app": {
                        "type": "string",
                        "description": "Restrict the search to one app id, e.g. `collectives`.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": FIND_MAX_LIMIT,
                        "description": f"Maximum operations to return. Defaults to {FIND_DEFAULT_LIMIT}.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name=DESCRIBE_TOOL_NAME,
            description=(
                f"Return the full JSON Schema of one or more operations found with "
                f"`{FIND_TOOL_NAME}`, so their arguments can be filled in correctly. "
                f"Ask for every operation you are about to use in a single call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"Operation names as returned by `{FIND_TOOL_NAME}`.",
                    }
                },
                "required": ["names"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name=CALL_TOOL_NAME,
            description=(
                f"Execute one Nextcloud API operation against the connected instance. "
                f"Look the operation up with `{FIND_TOOL_NAME}` and check its schema with "
                f"`{DESCRIBE_TOOL_NAME}` before calling, because `arguments` is passed "
                f"through unvalidated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": f"Operation name as returned by `{FIND_TOOL_NAME}`.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": (
                            "Arguments for the operation, shaped by the `input_schema` that "
                            f"`{DESCRIBE_TOOL_NAME}` returns for it."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
    ]


def build_request_body(definition: OperationDefinition, arguments: dict[str, Any]) -> tuple[Any, Any, Any]:
    if definition.body_mode == "none":
        return None, None, None

    if definition.body_mode == "flattened_json_object":
        body = {
            field_name: arguments[field_name]
            for field_name in definition.body_fields
            if field_name in arguments
        }
    else:
        body = arguments.get("body")

    if body is None:
        return None, None, None

    content_type = definition.body_content_type or "application/json"
    if "json" in content_type:
        return body, None, None
    if content_type == "application/x-www-form-urlencoded":
        return None, body, None
    if isinstance(body, str):
        return None, None, body
    return None, None, body


def parse_response_body(response: httpx2.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()

    if "json" in content_type:
        try:
            return {"data": response.json()}
        except ValueError:
            return {"data": response.text}

    if content_type.startswith("text/") or "xml" in content_type or "html" in content_type:
        return {"data": response.text}

    return {
        "data_base64": base64.b64encode(response.content).decode("ascii"),
        "encoding": "base64",
    }


async def execute_operation(
    definition: OperationDefinition,
    arguments: dict[str, Any],
    auth_context: AuthContext,
) -> dict[str, Any]:
    actual_path = definition.path
    for parameter_name in definition.path_params:
        if parameter_name not in arguments:
            raise ValueError(f"Missing required path parameter: {parameter_name}")
        actual_path = actual_path.replace(
            f"{{{parameter_name}}}",
            quote(str(arguments[parameter_name]), safe=""),
        )

    query_params = {
        parameter_name: arguments[parameter_name]
        for parameter_name in definition.query_params
        if parameter_name in arguments and arguments[parameter_name] is not None
    }
    headers = build_nextcloud_headers(auth_context)
    for parameter_name in definition.header_params:
        if parameter_name in arguments and arguments[parameter_name] is not None:
            headers[parameter_name] = str(arguments[parameter_name])

    json_body, form_body, raw_body = build_request_body(definition, arguments)
    if definition.body_content_type and "Content-Type" not in headers:
        headers["Content-Type"] = definition.body_content_type

    url = f"{NEXTCLOUD_URL}{actual_path}"
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method=definition.method,
            url=url,
            params=query_params,
            headers=headers,
            json=json_body,
            data=form_body,
            content=raw_body,
        )

    payload = {
        "ok": response.is_success,
        "status_code": response.status_code,
        "app_id": definition.app_id,
        "tool_name": definition.name,
        "method": definition.method,
        "path": definition.path,
        "resolved_url": url,
        "response_headers": safe_response_headers(response.headers),
    }
    payload.update(parse_response_body(response))
    return payload


def tool_result(payload: dict[str, Any], is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))],
        structured_content=payload,
        is_error=is_error,
    )


async def on_list_tools(
    ctx: ServerRequestContext[Any, Any],
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    await refresh_state()
    return types.ListToolsResult(tools=list_tools())


def find_result(arguments: dict[str, Any]) -> types.CallToolResult:
    requested_limit = arguments.get("limit") or FIND_DEFAULT_LIMIT
    try:
        limit = max(1, min(int(requested_limit), FIND_MAX_LIMIT))
    except (TypeError, ValueError):
        limit = FIND_DEFAULT_LIMIT

    matches, total = search_operations(arguments.get("query"), arguments.get("app"), limit)
    return tool_result(
        {
            "ok": True,
            "total_matches": total,
            "returned": len(matches),
            "truncated": total > len(matches),
            "operations": [operation_row(definition) for definition in matches],
        }
    )


def describe_result(arguments: dict[str, Any]) -> types.CallToolResult:
    names = arguments.get("names") or []
    if isinstance(names, str):
        names = [names]

    state = current_state()
    described: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    for name in names:
        definition = state.operations.get(name)
        if definition is None:
            unknown.append({"name": name, "did_you_mean": suggest_operation_names(name)})
            continue
        described.append(
            {
                **operation_row(definition),
                "app_id": definition.app_id,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
        )

    payload: dict[str, Any] = {"ok": not unknown, "operations": described}
    if unknown:
        payload["unknown"] = unknown
    return tool_result(payload, is_error=not described)


async def call_result(
    arguments: dict[str, Any],
    auth_context: AuthContext,
) -> types.CallToolResult:
    operation_name = arguments.get("name")
    definition = current_state().operations.get(operation_name)
    if definition is None:
        return tool_result(
            {
                "ok": False,
                "error": f"Unknown operation: {operation_name}",
                "did_you_mean": suggest_operation_names(operation_name),
            },
            is_error=True,
        )

    if not auth_context.auth_header:
        return tool_result(
            {
                "ok": False,
                "error": (
                    "Missing request credentials for this tool call. Configure MCP HTTP headers "
                    "`X-Nextcloud-Username` and `X-Nextcloud-AppToken`. "
                    "Server discovery credentials are not used for tool execution over HTTP."
                ),
                "tool_name": definition.name,
            },
            is_error=True,
        )

    try:
        payload = await execute_operation(definition, arguments.get("arguments") or {}, auth_context)
    except ValueError as exc:
        return tool_result({"ok": False, "error": str(exc), "tool_name": definition.name}, is_error=True)
    return tool_result(payload)


async def on_call_tool(
    ctx: ServerRequestContext[Any, Any],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    arguments = params.arguments or {}
    auth_context = execution_auth_context(ctx)

    if params.name == STATUS_TOOL_NAME:
        if arguments.get("refresh"):
            await refresh_state(force=True)
        return tool_result(discovery_status_payload(auth_context))

    await refresh_state()

    if params.name == FIND_TOOL_NAME:
        return find_result(arguments)
    if params.name == DESCRIBE_TOOL_NAME:
        return describe_result(arguments)
    if params.name == CALL_TOOL_NAME:
        return await call_result(arguments, auth_context)

    raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}")


@contextlib.asynccontextmanager
async def server_lifespan(_server):
    await refresh_state(force=True)
    yield {}


server = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    title="Nextcloud Live Instance MCP",
    description="Exposes the APIs of a connected Nextcloud instance as MCP tools.",
    lifespan=server_lifespan,
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
    cache_hints={"tools/list": CacheHint(ttl_ms=TOOL_LIST_TTL_MS, scope="private")},
)


async def healthcheck(_request) -> JSONResponse:
    state = current_state()
    return JSONResponse(
        {
            "name": "Nextcloud Live Instance MCP",
            "server_name": SERVER_NAME,
            "server_version": SERVER_VERSION,
            "mcp_path": "/mcp",
            "discovery_auth_configured": default_auth_context().auth_header is not None,
            "discovery_ok": state.last_refresh is not None and state.last_error is None,
            "app_count": len(state.apps),
            "tool_count": len(state.operations),
            "last_refresh": state.last_refresh,
        },
        status_code=200,
    )


def build_mcp_app():
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=MCP_HOST,
        debug=os.getenv("DEBUG", "").lower() == "true",
        custom_starlette_routes=[Route("/", endpoint=healthcheck, methods=["GET"])],
    )


def wrap_cors(asgi_app):
    return CORSMiddleware(
        asgi_app,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )


mcp_app = build_mcp_app()
app = wrap_cors(mcp_app)


async def run_stdio() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    if MCP_TRANSPORT == "stdio":
        asyncio.run(run_stdio())
        return

    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
