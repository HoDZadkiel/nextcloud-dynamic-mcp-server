import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote
from xml.etree import ElementTree

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
WEBDAV_APP_ID = "webdav"
# Files live outside the OCS API that ocs_api_viewer describes, so the webdav_*
# operations are hand-written rather than discovered. See `WebDAV` in the README.
WEBDAV_ROOT = "/remote.php/dav/files"
CALDAV_APP_ID = "caldav"
# Calendars are CalDAV (RFC 4791), not an OCS REST API, so ocs_api_viewer never
# sees them either - the caldav_* operations are hand-written for the same
# reason webdav_* is. See `Calendar` in the README.
CALDAV_ROOT = "/remote.php/dav/calendars"
DAV_NS = "{DAV:}"
CALDAV_NS = "{urn:ietf:params:xml:ns:caldav}"
CALENDARSERVER_NS = "{http://calendarserver.org/ns/}"
APPLE_ICAL_NS = "{http://apple.com/ns/ical/}"
ICAL_NEWLINE = "\r\n"
DEFAULT_EVENT_WINDOW_DAYS = 90
EVENT_LIST_DEFAULT_LIMIT = 100
EVENT_LIST_MAX_LIMIT = 500
ETAG_ENCODING_SUFFIXES = ("-gzip", "-br", "-deflate", "-zstd")
# Only the properties the tools actually report; asking for allprop would drag
# in Nextcloud's whole custom property set for no gain.
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getcontenttype/>"
    "<d:getlastmodified/><d:getetag/>"
    "</d:prop></d:propfind>"
)
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
    # Set only on the hand-written WebDAV entries, which are answered locally
    # instead of being proxied to an OCS endpoint. See BUILTIN_OPERATIONS.
    handler: str | None = None


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
    # WebDAV addresses a user's files as /remote.php/dav/files/<username>/...,
    # so the caller's own username has to survive alongside the encoded header.
    username: str | None = None


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
            username=NEXTCLOUD_USERNAME,
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
            username=username,
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


def all_operations() -> dict[str, OperationDefinition]:
    """The catalogue as callers see it: what discovery found, plus the
    hand-written WebDAV and CalDAV entries. Built-ins are merged at read time
    rather than written into DiscoveryState, so a refresh cannot drop them."""
    return {**BUILTIN_OPERATIONS, **current_state().operations}


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
    """Report status, disclosing the instance itself only to a credentialed caller.

    `GET /` is deliberately trimmed of the Nextcloud URL, the app inventory and
    raw discovery error text because it is reachable cross-origin. That trimming
    is worthless while this tool hands the same fields to anyone who can reach
    `/mcp`, so the same split is enforced here. What stays open is the caller's
    own auth diagnostics - exactly what someone whose credentials are not
    working needs in order to fix them.
    """
    discovery_auth_context = default_auth_context()
    state = current_state()
    payload: dict[str, Any] = {
        "discovery_auth_source": discovery_auth_context.source,
        "discovery_auth_configured": discovery_auth_context.auth_header is not None,
        "request_auth_source": request_context.source,
        "request_auth_configured": request_context.auth_header is not None,
        "supported_request_headers": [
            "X-Nextcloud-Username",
            "X-Nextcloud-AppToken",
        ],
        "app_count": len(state.apps),
        "tool_count": len(all_operations()),
        "last_refresh": state.last_refresh,
        "discovery_ok": state.last_refresh is not None and state.last_error is None,
        "discovery_retry_seconds": DISCOVERY_RETRY_SECONDS,
    }

    if request_context.auth_header:
        payload.update(
            {
                "nextcloud_url": NEXTCLOUD_URL,
                "api_viewer_url": API_VIEWER_URL,
                "apps": state.apps,
                "last_error": state.last_error,
            }
        )
    return payload


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
    for definition in all_operations().values():
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
    for definition in all_operations().values():
        score = term_score(definition, terms)
        if score:
            scored.append((-score, definition.name))

    scored.sort()
    return [item[1] for item in scored[:SUGGESTION_LIMIT]]


def app_index() -> str:
    """Includes the built-in `webdav` and `caldav` apps, which are not discovered
    and would otherwise be invisible to anyone who did not already know they
    were there."""
    builtin_counts: dict[str, int] = {}
    for definition in BUILTIN_OPERATIONS.values():
        builtin_counts[definition.app_id] = builtin_counts.get(definition.app_id, 0) + 1
    entries = [f"{app_id}:{count}" for app_id, count in sorted(builtin_counts.items())]
    entries += [f"{app['id']}:{app['operation_count']}" for app in current_state().apps]
    return ", ".join(entries)


def find_tool_description(authenticated: bool) -> str:
    """The catalogue overview is instance-specific, so only credentialed
    callers get it. `tools/list` itself stays open - a client that cannot list
    tools cannot connect at all - but what an anonymous caller learns is then
    limited to the four tool names, which the public repository already
    documents."""
    shared = (
        f"Start here, then call `{DESCRIBE_TOOL_NAME}` for the arguments of the "
        f"operations you picked, then `{CALL_TOOL_NAME}` to run one. "
        f"Use this only for real operations against the configured Nextcloud server, "
        f"not for documentation lookup or local workspace tasks."
    )
    if not authenticated:
        return (
            "Search the live Nextcloud API operations available on the connected "
            "instance and return their names, HTTP method, path and summary. "
            f"{shared} Requires the `X-Nextcloud-Username` and `X-Nextcloud-AppToken` "
            "request headers."
        )
    return (
        f"Search the {len(all_operations())} live Nextcloud API operations "
        f"available on the connected instance and return their names, HTTP method, "
        f"path and summary. {shared} "
        f"Apps on this instance (id:operations) - {app_index()}."
    )


def list_tools(authenticated: bool = False) -> list[types.Tool]:
    """Publish a fixed four-tool surface instead of one tool per operation.

    An instance with every app enabled discovers ~550 operations. Publishing
    those as ~550 tools puts roughly 100k tokens of schema into every client's
    context before a single call is made, and no session uses more than a
    handful of them. The catalogue is searched on demand instead, which costs
    one extra round trip and about 1k tokens of context.
    """
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
            description=find_tool_description(authenticated),
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


def webdav_path_segments(path: str, allow_root: bool = False) -> list[str]:
    """Split a user-relative path, rejecting traversal.

    `.` and `..` are refused rather than normalised: the caller is already
    confined to their own files by Basic auth, but a traversal could still climb
    out of `/remote.php/dav/files/<user>/` and address other DAV endpoints.
    """
    segments = [segment for segment in str(path or "").replace("\\", "/").split("/") if segment]
    if not segments and not allow_root:
        raise ValueError("Path is required")
    for segment in segments:
        if segment in (".", ".."):
            raise ValueError(f"Path segment not allowed: {segment}")
    return segments


def webdav_url(auth_context: AuthContext, path: str, allow_root: bool = False) -> str:
    """Resolve a user-relative path to its WebDAV URL.

    Each segment is encoded separately so that spaces and non-ASCII names -
    `.Collectives/研究筆記/第一章 概論/…` is an ordinary shape here - survive intact
    while the separators stay literal.
    """
    if not auth_context.username:
        raise ValueError("Cannot resolve a WebDAV path without the caller's username")

    root = f"{NEXTCLOUD_URL}{WEBDAV_ROOT}/{quote(auth_context.username, safe='')}"
    segments = webdav_path_segments(path, allow_root=allow_root)
    if not segments:
        return root
    return f"{root}/" + "/".join(quote(segment, safe="") for segment in segments)


def parse_propfind(body: str, auth_context: AuthContext) -> list[dict[str, Any]]:
    """Turn a PROPFIND multistatus into plain entries.

    Paths are reported back relative to the user's files root, so that whatever
    comes out of a listing can be fed straight into the other file tools without
    the caller having to strip `/remote.php/dav/files/<user>/` by hand.
    """
    dav = "{DAV:}"
    prefix = f"{WEBDAV_ROOT}/{quote(auth_context.username or '', safe='')}"
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Could not parse the PROPFIND response: {exc}") from exc

    entries: list[dict[str, Any]] = []
    for element in root.findall(f"{dav}response"):
        href = (element.findtext(f"{dav}href") or "").strip()
        relative = unquote(href)
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
        relative = relative.strip("/")

        properties = element.find(f"{dav}propstat/{dav}prop")
        if properties is None:
            continue
        size = properties.findtext(f"{dav}getcontentlength")
        entries.append(
            {
                "path": relative,
                "name": relative.rsplit("/", 1)[-1],
                "is_folder": properties.find(f"{dav}resourcetype/{dav}collection") is not None,
                "size": int(size) if size and size.isdigit() else None,
                "content_type": properties.findtext(f"{dav}getcontenttype") or None,
                "last_modified": properties.findtext(f"{dav}getlastmodified") or None,
                "etag": normalize_etag(properties.findtext(f"{dav}getetag")),
            }
        )
    return entries


def normalize_etag(etag: str | None) -> str | None:
    """Strip the transfer-encoding suffix a compressing server appends.

    Apache and nginx mangle the ETag when they compress a response: the entity
    whose validator is `"abc"` comes back from a GET as `"abc-gzip"`. Handing
    that straight back as `If-Match` is refused with 412 even though nothing
    changed, and the obvious recovery - read again, retry - returns the same
    mangled value and fails identically, forever. Observed against a live
    instance, so the whole guarded-write workflow is unusable behind a
    compressing proxy without this.
    """
    if not etag:
        return etag

    prefix, value = ("W/", etag[2:]) if etag.startswith("W/") else ("", etag)
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return etag

    inner = value[1:-1]
    for suffix in ETAG_ENCODING_SUFFIXES:
        if inner.endswith(suffix):
            inner = inner[: -len(suffix)]
            break
    return f'{prefix}"{inner}"'


def build_webdav_headers(
    auth_context: AuthContext,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {"Accept": "*/*"}
    if auth_context.auth_header:
        headers["Authorization"] = auth_context.auth_header
    if extra_headers:
        headers.update(extra_headers)
    return headers


def missing_parent_error(path: str) -> str:
    return (
        f"No parent folder for `{path}`. Nextcloud answers a write below a missing "
        f"folder with 404, not the 409 the WebDAV spec suggests. Create the folder "
        "with `webdav_create_folder` first, or check the path for a typo."
    )


def looks_like_a_collection(response: httpx2.Response) -> bool:
    """Decide whether a successful GET actually addressed a folder.

    WebDAV leaves GET on a collection undefined and Nextcloud answers it with
    200 and an HTML blurb about the WebDAV interface, so an unguarded read of a
    folder path returns a plausible-looking file whose contents are that
    sentence. Files always carry an ETag here and this page never does, which is
    the cheap half of the signal; the HTML content type is the other half.
    """
    if not response.is_success or response.headers.get("etag"):
        return False
    return "text/html" in response.headers.get("content-type", "").lower()


async def read_file(auth_context: AuthContext, path: str) -> dict[str, Any]:
    url = webdav_url(auth_context, path)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=build_webdav_headers(auth_context))

    if looks_like_a_collection(response):
        return {
            "ok": False,
            "status_code": response.status_code,
            "path": path,
            "error": (
                f"`{path}` is a folder, not a file. Nextcloud answers a folder read "
                f"with 200 and a placeholder page rather than an error, so this would "
                f"otherwise look like a successful read. Use "
                "`webdav_list_directory` to see what is inside it."
            ),
        }

    payload = {
        "ok": response.is_success,
        "status_code": response.status_code,
        "path": path,
        # Carry the ETag out so it can be handed straight back as `if_match`.
        "etag": normalize_etag(response.headers.get("etag")),
        "response_headers": safe_response_headers(response.headers),
    }
    if response.status_code == 404:
        payload["error"] = f"No file at `{path}`."
    payload.update(parse_response_body(response))
    return payload


def write_payload_bytes(content: Any, content_base64: str | None) -> bytes:
    """Resolve the two mutually exclusive body inputs to bytes.

    `content_base64` exists because reads hand binary back base64-encoded. With
    only a text field, feeding that straight back writes the base64 *text* to the
    file and the next read encodes it a second time - a silent round-trip
    corruption that reports success at every step.
    """
    if content_base64 is not None:
        if content:
            raise ValueError("Pass either `content` or `content_base64`, not both")
        try:
            return base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"`content_base64` is not valid base64: {exc}") from exc
    return str(content or "").encode("utf-8")


async def write_file(
    auth_context: AuthContext,
    path: str,
    content: Any = None,
    if_match: str | None = None,
    content_base64: str | None = None,
) -> dict[str, Any]:
    """Replace a file's contents.

    `if_match` is the ETag a prior read returned. Sending it makes the write
    conditional, so an edit computed from stale content fails with 412 instead
    of silently discarding whatever changed in between - the difference between
    a lost paragraph and a retry.
    """
    body = write_payload_bytes(content, content_base64)
    url = webdav_url(auth_context, path)
    extra_headers = {"If-Match": normalize_etag(if_match)} if if_match else None
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.put(
            url,
            headers=build_webdav_headers(auth_context, extra_headers),
            content=body,
        )

    payload: dict[str, Any] = {
        "ok": response.is_success,
        "status_code": response.status_code,
        "path": path,
        "bytes_written": len(body) if response.is_success else 0,
        "etag": normalize_etag(response.headers.get("etag")),
        "response_headers": safe_response_headers(response.headers),
    }
    if response.status_code == 412:
        payload["error"] = (
            "The file changed since the `if_match` ETag was read. Read it again, "
            "reapply the edit to the current content, and retry."
        )
    elif response.status_code in (404, 409):
        payload["error"] = missing_parent_error(path)
    elif not response.is_success:
        payload.update(parse_response_body(response))
    return payload


async def create_folder(
    auth_context: AuthContext,
    path: str,
    parents: bool = True,
) -> dict[str, Any]:
    """Create a folder, optionally the whole chain leading to it.

    MKCOL makes exactly one level and fails with 409 when an intermediate is
    missing, which is the same dead end a write hits. Walking the chain is what
    makes uploading a directory tree possible at all. An existing folder answers
    405; that is reported as success with `created: false` so a caller looping
    over a tree does not have to distinguish "made it" from "already there".
    """
    segments = webdav_path_segments(path)
    targets = [segments] if not parents else [segments[: i + 1] for i in range(len(segments))]
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)
    created: list[str] = []
    existed: list[str] = []

    async with httpx2.AsyncClient(timeout=timeout) as client:
        for target in targets:
            target_path = "/".join(target)
            response = await client.request(
                "MKCOL",
                webdav_url(auth_context, target_path),
                headers=build_webdav_headers(auth_context),
            )
            if response.is_success:
                created.append(target_path)
            elif response.status_code == 405:
                existed.append(target_path)
            else:
                payload = {
                    "ok": False,
                    "status_code": response.status_code,
                    "path": path,
                    "failed_at": target_path,
                    "created": created,
                }
                if response.status_code == 409:
                    payload["error"] = (
                        f"`{target_path}` has no parent folder. Retry with "
                        f"`parents` set to true to create the whole chain."
                    )
                else:
                    payload.update(parse_response_body(response))
                return payload

    return {
        "ok": True,
        "status_code": 201 if created else 405,
        "path": path,
        "created": created,
        "already_existed": existed,
    }


async def delete_path(
    auth_context: AuthContext,
    path: str,
    recursive: bool = False,
) -> dict[str, Any]:
    """Delete a file, or a folder and everything under it.

    WebDAV DELETE on a collection is unconditionally recursive - there is no
    shallow variant to fall back on - so the resource is inspected first and a
    folder is refused unless `recursive` says so explicitly. One extra round trip
    is a cheap price for not letting a mistyped path take a subtree with it.
    """
    url = webdav_url(auth_context, path)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        probe = await client.request(
            "PROPFIND",
            url,
            headers=build_webdav_headers(auth_context, {"Depth": "0"}),
            content=PROPFIND_BODY,
        )
        if probe.status_code == 404:
            return {"ok": False, "status_code": 404, "path": path, "error": f"Nothing at `{path}`."}

        entries = parse_propfind(probe.text, auth_context)
        is_collection = bool(entries and entries[0]["is_folder"])
        if is_collection and not recursive:
            return {
                "ok": False,
                "status_code": probe.status_code,
                "path": path,
                "is_folder": True,
                "error": (
                    f"`{path}` is a folder. Deleting it removes everything inside, and "
                    f"WebDAV offers no shallow delete, so pass `recursive` as true to "
                    f"confirm that is intended."
                ),
            }

        response = await client.request("DELETE", url, headers=build_webdav_headers(auth_context))

    payload: dict[str, Any] = {
        "ok": response.is_success,
        "status_code": response.status_code,
        "path": path,
        "is_folder": is_collection,
    }
    if not response.is_success:
        payload.update(parse_response_body(response))
    return payload


async def list_directory(auth_context: AuthContext, path: str = "") -> dict[str, Any]:
    """List a folder's immediate children, files included.

    `files_api_get_folder_tree` in the discovered catalogue returns folders only,
    so it cannot answer "what is in here" - PROPFIND can.
    """
    url = webdav_url(auth_context, path, allow_root=True)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            "PROPFIND",
            url,
            headers=build_webdav_headers(auth_context, {"Depth": "1"}),
            content=PROPFIND_BODY,
        )

    if not response.is_success:
        payload: dict[str, Any] = {"ok": False, "status_code": response.status_code, "path": path}
        if response.status_code == 404:
            payload["error"] = f"No folder at `{path}`."
        else:
            payload.update(parse_response_body(response))
        return payload

    entries = parse_propfind(response.text, auth_context)
    # The first entry is the folder itself; the caller asked what is inside it.
    return {
        "ok": True,
        "status_code": response.status_code,
        "path": path,
        "entries": entries[1:],
    }


WEBDAV_HANDLERS = {
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "create_folder": create_folder,
    "delete_path": delete_path,
}

PATH_PROPERTY = {"type": "string", "description": "Path relative to the caller's files root."}


# --- CalDAV (calendars) -------------------------------------------------------
#
# Same rationale as WebDAV above: calendars are CalDAV (RFC 4791), which lives
# outside anything ocs_api_viewer's OpenAPI documents describe, so listing
# calendars, reading events and writing them all have to be hand-written. Only
# VEVENT (ordinary calendar events) is covered - VTODO and VJOURNAL are not.
#
# Event identity: a calendar object's filename is not guaranteed to match its
# UID (sabre/dav, the library Nextcloud's CalDAV server is built on, says so
# explicitly), so every operation that addresses a specific event takes the
# `path` a prior list/get/create already returned rather than one composed
# from a UID by hand - the same pattern `webdav_*` uses for file paths.


def ical_escape_text(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT escaping. Order matters: backslashes first, or a
    backslash introduced by a later replacement would get escaped again."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def ical_unescape_text(value: str) -> str:
    return re.sub(r"\\(.)", lambda match: "\n" if match.group(1) in "nN" else match.group(1), value)


def fold_ical_line(line: str) -> str:
    """RFC 5545 §3.1: a content line over 75 octets is folded into CRLF plus a
    leading space per continuation, without splitting a multi-byte UTF-8
    sequence across the break."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line

    def safe_end(start: int, limit: int) -> int:
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        return end

    end = safe_end(0, 75)
    parts = [data[0:end].decode("utf-8")]
    pos = end
    while pos < len(data):
        end = safe_end(pos, 74)  # 74 + the leading continuation space = 75
        parts.append(" " + data[pos:end].decode("utf-8"))
        pos = end
    return ICAL_NEWLINE.join(parts)


def unfold_ical_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith(" ") or raw.startswith("\t"):
            if lines:
                lines[-1] += raw[1:]
            continue
        if raw:
            lines.append(raw)
    return lines


def ical_param_value(value: str) -> str:
    """RFC 5545 §3.2 parameter values are not TEXT: a backslash is not an escape
    here, so `ical_escape_text` would leave a literal `\\,` behind. A value with
    `,` `;` or `:` is double-quoted instead, and a `"` (which cannot appear at
    all) or line break is dropped."""
    cleaned = re.sub(r'["\r\n]', "", str(value))
    return f'"{cleaned}"' if re.search(r"[,;:]", cleaned) else cleaned


def split_outside_quotes(text: str, separator: str, maxsplit: int = -1) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
        if char == separator and not in_quotes and maxsplit != 0:
            parts.append("".join(current))
            current = []
            maxsplit -= 1
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def parse_ical_property(line: str) -> tuple[str, dict[str, str], str]:
    split = split_outside_quotes(line, ":", 1)
    if len(split) < 2:
        return line.upper(), {}, ""
    head, value = split
    parts = split_outside_quotes(head, ";")
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        if key:
            params[key.upper()] = val.strip('"')
    return parts[0].upper(), params, value


def extract_blocks(lines: list[str], component: str) -> list[list[str]]:
    """Pull out the inner lines of every top-level `BEGIN:{component}` ...
    `END:{component}` block. Not recursive - VALARM never nests VALARM, and
    VEVENT blocks are addressed one at a time, so one level is enough."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    depth = 0
    for line in lines:
        name, _, value = line.partition(":")
        bare_name = name.split(";")[0].upper()
        if bare_name == "BEGIN" and value.strip().upper() == component:
            if depth == 0:
                current = []
            depth += 1
            continue
        if bare_name == "END" and value.strip().upper() == component:
            depth -= 1
            if depth == 0 and current is not None:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def strip_blocks(lines: list[str], component: str) -> list[str]:
    result: list[str] = []
    depth = 0
    for line in lines:
        name, _, value = line.partition(":")
        bare_name = name.split(";")[0].upper()
        if bare_name == "BEGIN" and value.strip().upper() == component:
            depth += 1
            continue
        if bare_name == "END" and value.strip().upper() == component:
            depth -= 1
            continue
        if depth == 0:
            result.append(line)
    return result


def encode_ical_datetime(value: Any, field: str) -> tuple[str, bool]:
    """Returns (iCalendar value, is_date_only). A bare local datetime is
    rejected rather than guessed at - CalDAV has no notion of the caller's
    timezone, so there is no safe default to fall back to."""
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", ""), True
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"`{field}` must be `YYYY-MM-DD` for an all-day event, or a full ISO-8601 "
            f"datetime with a UTC offset or `Z` (e.g. `2026-09-20T14:00:00+08:00`): {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"`{field}` needs a UTC offset or `Z` - a bare local datetime is ambiguous "
            f"without knowing the calendar's timezone."
        )
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), False


def decode_ical_datetime(value: str, params: dict[str, str]) -> dict[str, Any] | None:
    """The inverse of `encode_ical_datetime`, plus the cases a server can hand
    back that a caller never sends: a `TZID`-qualified or floating local time,
    which is returned as the naive wall-clock value plus the zone name rather
    than converted to an absolute instant - that would need the event's
    `VTIMEZONE` table, which this server does not parse."""
    if not value:
        return None
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        return {"value": f"{value[0:4]}-{value[4:6]}-{value[6:8]}", "all_day": True, "timezone": None}
    if value.endswith("Z"):
        try:
            parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return {"value": value, "all_day": False, "timezone": None}
        return {"value": parsed.isoformat(), "all_day": False, "timezone": "UTC"}
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return {"value": value, "all_day": False, "timezone": params.get("TZID")}
    return {"value": parsed.isoformat(), "all_day": False, "timezone": params.get("TZID")}


def parse_utc_ical_timestamp(value: str) -> str | None:
    """CREATED/LAST-MODIFIED/DTSTAMP are always UTC per RFC 5545, unlike
    DTSTART/DTEND - so these come back as a plain ISO string rather than the
    `{value, all_day, timezone}` shape a possibly-floating time needs."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return value


def parse_vevent_block(lines: list[str]) -> dict[str, Any]:
    alarm_blocks = extract_blocks(lines, "VALARM")
    own_lines = strip_blocks(lines, "VALARM")

    props: dict[str, list[tuple[dict[str, str], str]]] = {}
    for line in own_lines:
        name, params, value = parse_ical_property(line)
        props.setdefault(name, []).append((params, ical_unescape_text(value)))

    def first(name: str, default: str = "") -> str:
        return props[name][0][1] if name in props else default

    def first_params(name: str) -> dict[str, str]:
        return props[name][0][0] if name in props else {}

    def mailto_entry(params: dict[str, str], value: str) -> dict[str, Any]:
        email = value[7:] if value.lower().startswith("mailto:") else value
        return {"email": email, "name": params.get("CN")}

    organizer_entries = props.get("ORGANIZER", [])
    reminders: list[dict[str, Any]] = []
    for alarm_lines in alarm_blocks:
        alarm_props: dict[str, str] = {}
        for line in alarm_lines:
            name, _, value = parse_ical_property(line)
            alarm_props[name] = value
        trigger = alarm_props.get("TRIGGER", "")
        match = re.fullmatch(r"-PT(\d+)([HM])", trigger)
        minutes_before = (int(match.group(1)) * (60 if match.group(2) == "H" else 1)) if match else None
        reminders.append({"trigger": trigger or None, "minutes_before": minutes_before})

    return {
        "uid": first("UID") or None,
        "recurrence_id": first("RECURRENCE-ID") or None,
        "summary": first("SUMMARY") or None,
        "description": first("DESCRIPTION") or None,
        "location": first("LOCATION") or None,
        "status": first("STATUS") or None,
        "start": decode_ical_datetime(first("DTSTART"), first_params("DTSTART")),
        "end": decode_ical_datetime(first("DTEND"), first_params("DTEND")),
        "rrule": first("RRULE") or None,
        "categories": [c.strip() for c in first("CATEGORIES").split(",") if c.strip()],
        "organizer": mailto_entry(*organizer_entries[0]) if organizer_entries else None,
        "attendees": [mailto_entry(params, value) for params, value in props.get("ATTENDEE", [])],
        "reminders": reminders,
        "sequence": int(first("SEQUENCE", "0") or 0),
        "created": parse_utc_ical_timestamp(first("CREATED")),
        "last_modified": parse_utc_ical_timestamp(first("LAST-MODIFIED")),
    }


def build_vevent_ics(uid: str, fields: dict[str, Any], created: str | None = None) -> str:
    """Build a complete VCALENDAR/VEVENT. Always a full document, never a
    patch - `caldav_update_event` is a full replace for the same reason
    `webdav_write_file` is, so there is exactly one code path that has to get
    the iCalendar syntax right. `created` carries the original creation time
    (an ISO string, as `parse_utc_ical_timestamp` returns it) through an update;
    without it, or if it does not parse, the event is stamped as created now."""
    summary = fields.get("summary")
    if not summary:
        raise ValueError("`summary` is required")
    start = fields.get("start")
    if not start:
        raise ValueError("`start` is required")

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_value, start_is_date = encode_ical_datetime(start, "start")
    created_value = now
    if created:
        try:
            created_value = datetime.fromisoformat(created).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            pass

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nextcloud-dynamic-mcp-server-fork//caldav//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"CREATED:{created_value}",
        f"LAST-MODIFIED:{now}",
        "SEQUENCE:0",
        f"DTSTART{';VALUE=DATE' if start_is_date else ''}:{start_value}",
    ]

    end = fields.get("end")
    if end is not None:
        end_value, end_is_date = encode_ical_datetime(end, "end")
        if end_is_date != start_is_date:
            raise ValueError("`start` and `end` must both be `YYYY-MM-DD` dates or both be datetimes")
        if end_value < start_value:  # same format on both sides, so string order is time order
            raise ValueError("`end` must not be earlier than `start`")
        lines.append(f"DTEND{';VALUE=DATE' if end_is_date else ''}:{end_value}")
    elif not start_is_date:
        raise ValueError("`end` is required for a timed event - only an all-day event may omit it")

    lines.append(f"SUMMARY:{ical_escape_text(summary)}")
    if fields.get("description"):
        lines.append(f"DESCRIPTION:{ical_escape_text(fields['description'])}")
    if fields.get("location"):
        lines.append(f"LOCATION:{ical_escape_text(fields['location'])}")
    if fields.get("status"):
        status = str(fields["status"]).upper()
        if status not in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
            raise ValueError("`status` must be one of CONFIRMED, TENTATIVE, CANCELLED")
        lines.append(f"STATUS:{status}")
    if fields.get("categories"):
        lines.append(f"CATEGORIES:{','.join(ical_escape_text(c) for c in fields['categories'])}")
    if fields.get("rrule"):
        rrule = str(fields["rrule"])
        if any(ch in rrule for ch in "\r\n:"):
            raise ValueError(
                "`rrule` must be a single RFC 5545 value with no colon or newline, "
                "e.g. `FREQ=WEEKLY;COUNT=10`"
            )
        if not re.fullmatch(r"FREQ=[A-Za-z]+(;[A-Za-z-]+=[^;]+)*", rrule):
            raise ValueError(
                f"`rrule` is not a valid RFC 5545 recurrence rule: {rrule!r}. It must start with "
                "`FREQ=` followed by `NAME=value` parts, e.g. `FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10`"
            )
        lines.append(f"RRULE:{rrule}")
    if fields.get("organizer_email"):
        cn = f";CN={ical_param_value(fields['organizer_name'])}" if fields.get("organizer_name") else ""
        lines.append(f"ORGANIZER{cn}:mailto:{fields['organizer_email']}")
    for attendee in fields.get("attendees") or []:
        email = (attendee or {}).get("email")
        if not email:
            raise ValueError("Every entry in `attendees` needs an `email`")
        name = attendee.get("name")
        cn = f";CN={ical_param_value(name)}" if name else ""
        lines.append(f"ATTENDEE{cn}:mailto:{email}")
    for minutes in fields.get("reminders_minutes_before") or []:
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            raise ValueError(f"`reminders_minutes_before` entries must be integers, got: {minutes!r}") from None
        if minutes_int < 0:
            raise ValueError("`reminders_minutes_before` entries must not be negative")
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{ical_escape_text(summary)}",
                f"TRIGGER:-PT{minutes_int}M",
                "END:VALARM",
            ]
        )

    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return ICAL_NEWLINE.join(fold_ical_line(line) for line in lines) + ICAL_NEWLINE


def xml_escape(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def caldav_home_url(auth_context: AuthContext) -> str:
    if not auth_context.username:
        raise ValueError("Cannot resolve a calendar path without the caller's username")
    return f"{NEXTCLOUD_URL}{CALDAV_ROOT}/{quote(auth_context.username, safe='')}"


def caldav_calendar_id(calendar: Any) -> str:
    segments = webdav_path_segments(str(calendar or ""))
    if len(segments) != 1:
        raise ValueError("`calendar` must be a single calendar id, e.g. `personal` - not a path")
    return segments[0]


def caldav_calendar_url(auth_context: AuthContext, calendar: str) -> str:
    return f"{caldav_home_url(auth_context)}/{quote(caldav_calendar_id(calendar), safe='')}"


def caldav_object_url(auth_context: AuthContext, calendar: str, path: str) -> str:
    """`path` is what `caldav_list_events`, `caldav_get_event` or
    `caldav_create_event` returned - not composed from a UID by hand. See the
    note at the top of this section."""
    segments = webdav_path_segments(str(path or ""))
    return f"{caldav_calendar_url(auth_context, calendar)}/" + "/".join(quote(s, safe="") for s in segments)


CALDAV_LIST_CALENDARS_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/" '
    'xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:ic="http://apple.com/ns/ical/">'
    "<d:prop>"
    "<d:resourcetype/><d:displayname/><cs:getctag/>"
    "<c:supported-calendar-component-set/><ic:calendar-color/>"
    "<d:current-user-privilege-set/>"
    "</d:prop></d:propfind>"
)


async def list_calendars(auth_context: AuthContext) -> dict[str, Any]:
    """List the caller's calendars - the CalDAV analogue of
    `webdav_list_directory`. Filtered to entries whose resourcetype actually
    includes CALDAV:calendar, which drops the home collection itself along
    with the scheduling inbox/outbox Nextcloud also keeps under this path."""
    url = caldav_home_url(auth_context)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            "PROPFIND",
            url,
            headers=build_webdav_headers(
                auth_context, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
            ),
            content=CALDAV_LIST_CALENDARS_BODY,
        )

    if not response.is_success:
        payload: dict[str, Any] = {"ok": False, "status_code": response.status_code}
        payload.update(parse_response_body(response))
        return payload

    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Could not parse the calendar list response: {exc}") from exc

    prefix = f"{CALDAV_ROOT}/{quote(auth_context.username or '', safe='')}"
    calendars: list[dict[str, Any]] = []
    for element in root.findall(f"{DAV_NS}response"):
        prop = element.find(f"{DAV_NS}propstat/{DAV_NS}prop")
        if prop is None:
            continue
        resourcetype = prop.find(f"{DAV_NS}resourcetype")
        if resourcetype is None or resourcetype.find(f"{CALDAV_NS}calendar") is None:
            continue

        href = unquote((element.findtext(f"{DAV_NS}href") or "").strip())
        relative = href[len(prefix):].strip("/") if href.startswith(prefix) else href.strip("/")

        privilege_set = prop.find(f"{DAV_NS}current-user-privilege-set")
        writable = (
            privilege_set is None
            or privilege_set.find(f".//{DAV_NS}write") is not None
            or privilege_set.find(f".//{DAV_NS}all") is not None
        )
        components_el = prop.find(f"{CALDAV_NS}supported-calendar-component-set")
        components = (
            [comp.get("name") for comp in components_el.findall(f"{CALDAV_NS}comp")]
            if components_el is not None
            else []
        )
        calendars.append(
            {
                "id": relative,
                "display_name": prop.findtext(f"{DAV_NS}displayname") or relative,
                "color": prop.findtext(f"{APPLE_ICAL_NS}calendar-color"),
                "ctag": prop.findtext(f"{CALENDARSERVER_NS}getctag"),
                "components": components,
                "writable": writable,
            }
        )
    return {"ok": True, "status_code": response.status_code, "calendars": calendars}


def format_time_bound(value: Any, field: str) -> str:
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"`{field}` is not a valid `YYYY-MM-DD` date or ISO-8601 datetime: {exc}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_calendar_query_body(time_range_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f"{time_range_xml}"
        "</c:comp-filter></c:comp-filter></c:filter>"
        "</c:calendar-query>"
    )


async def list_events(
    auth_context: AuthContext,
    calendar: str | None = None,
    time_min: str | None = None,
    time_max: str | None = None,
    all_time: bool = False,
    limit: Any = None,
) -> dict[str, Any]:
    """List events, from one `calendar` or across every calendar the caller can
    see. Bounded to a time window by default - a CalDAV REPORT has no
    pagination, so an unbounded query against a calendar with years of
    recurring events could return all of it."""
    try:
        capped_limit = max(1, min(int(limit or EVENT_LIST_DEFAULT_LIMIT), EVENT_LIST_MAX_LIMIT))
    except (TypeError, ValueError):
        capped_limit = EVENT_LIST_DEFAULT_LIMIT

    if calendar:
        calendar_ids = [caldav_calendar_id(calendar)]
    else:
        listing = await list_calendars(auth_context)
        if not listing.get("ok"):
            return listing
        calendar_ids = [entry["id"] for entry in listing["calendars"]]

    time_range_xml = ""
    if not all_time:
        now = datetime.now(timezone.utc)
        start = format_time_bound(time_min, "time_min") if time_min else now.strftime("%Y%m%dT%H%M%SZ")
        end = (
            format_time_bound(time_max, "time_max")
            if time_max
            else (now + timedelta(days=DEFAULT_EVENT_WINDOW_DAYS)).strftime("%Y%m%dT%H%M%SZ")
        )
        time_range_xml = f'<c:time-range start="{start}" end="{end}"/>'
    body = build_calendar_query_body(time_range_xml)

    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    async with httpx2.AsyncClient(timeout=timeout) as client:
        for calendar_id in calendar_ids:
            url = caldav_calendar_url(auth_context, calendar_id)
            response = await client.request(
                "REPORT",
                url,
                headers=build_webdav_headers(
                    auth_context, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
                ),
                content=body,
            )
            if response.status_code == 404:
                continue
            if not response.is_success:
                errors.append({"calendar": calendar_id, "status_code": response.status_code})
                continue
            try:
                root = ElementTree.fromstring(response.text)
            except ElementTree.ParseError as exc:
                errors.append({"calendar": calendar_id, "error": f"Could not parse response: {exc}"})
                continue

            prefix = f"{CALDAV_ROOT}/{quote(auth_context.username or '', safe='')}/{quote(calendar_id, safe='')}"
            for element in root.findall(f"{DAV_NS}response"):
                prop = element.find(f"{DAV_NS}propstat/{DAV_NS}prop")
                if prop is None:
                    continue
                calendar_data = prop.findtext(f"{CALDAV_NS}calendar-data")
                if not calendar_data:
                    continue
                href = unquote((element.findtext(f"{DAV_NS}href") or "").strip())
                relative = href[len(prefix):].strip("/") if href.startswith(prefix) else href.rsplit("/", 1)[-1]

                blocks = extract_blocks(unfold_ical_lines(calendar_data), "VEVENT")
                if not blocks:
                    continue
                master = next(
                    (b for b in blocks if not any(line.upper().startswith("RECURRENCE-ID") for line in b)),
                    blocks[0],
                )
                events.append(
                    {
                        "calendar": calendar_id,
                        "path": relative,
                        "etag": normalize_etag(prop.findtext(f"{DAV_NS}getetag")),
                        "override_count": len(blocks) - 1,
                        **parse_vevent_block(master),
                    }
                )

    events.sort(key=lambda event: ((event.get("start") or {}).get("value") or ""))
    return {
        "ok": True,
        "calendars_searched": calendar_ids,
        "total_matches": len(events),
        "returned": min(len(events), capped_limit),
        "truncated": len(events) > capped_limit,
        "events": events[:capped_limit],
        "errors": errors or None,
    }


async def get_event(auth_context: AuthContext, calendar: str = None, path: str = None) -> dict[str, Any]:
    if not calendar or not path:
        raise ValueError("`calendar` and `path` are required")
    url = caldav_object_url(auth_context, calendar, path)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=build_webdav_headers(auth_context))

    if response.status_code == 404:
        return {
            "ok": False, "status_code": 404, "calendar": calendar, "path": path,
            "error": f"No event at `{path}` in calendar `{calendar}`.",
        }
    if not response.is_success:
        payload: dict[str, Any] = {"ok": False, "status_code": response.status_code, "calendar": calendar, "path": path}
        payload.update(parse_response_body(response))
        return payload

    blocks = extract_blocks(unfold_ical_lines(response.text), "VEVENT")
    if not blocks:
        return {
            "ok": False, "status_code": response.status_code, "calendar": calendar, "path": path,
            "error": "This calendar object has no VEVENT - it may be a VTODO or VJOURNAL, which "
                     "this server does not parse.",
        }

    parsed_blocks = [parse_vevent_block(block) for block in blocks]
    master = next((entry for entry in parsed_blocks if not entry.get("recurrence_id")), parsed_blocks[0])
    return {
        "ok": True,
        "status_code": response.status_code,
        "calendar": calendar,
        "path": path,
        "etag": normalize_etag(response.headers.get("etag")),
        **master,
        "overrides": [entry for entry in parsed_blocks if entry is not master],
        "raw_ics": response.text,
    }


async def create_event(
    auth_context: AuthContext,
    calendar: str = None,
    summary: str = None,
    start: str = None,
    end: str = None,
    description: str = None,
    location: str = None,
    status: str = None,
    categories: list[str] = None,
    rrule: str = None,
    organizer_email: str = None,
    organizer_name: str = None,
    attendees: list[Any] = None,
    reminders_minutes_before: list[Any] = None,
    uid: str = None,
) -> dict[str, Any]:
    if not calendar:
        raise ValueError("`calendar` is required")
    fields = dict(
        summary=summary, start=start, end=end, description=description, location=location,
        status=status, categories=categories, rrule=rrule, organizer_email=organizer_email,
        organizer_name=organizer_name, attendees=attendees, reminders_minutes_before=reminders_minutes_before,
    )
    event_uid = str(uid) if uid else str(uuid.uuid4())
    ics = build_vevent_ics(event_uid, fields)
    path = f"{event_uid}.ics"
    url = caldav_object_url(auth_context, calendar, path)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.put(
            url,
            headers=build_webdav_headers(
                auth_context, {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}
            ),
            content=ics.encode("utf-8"),
        )

    payload: dict[str, Any] = {
        "ok": response.is_success, "status_code": response.status_code,
        "calendar": calendar, "path": path, "uid": event_uid,
        "etag": normalize_etag(response.headers.get("etag")),
    }
    if response.status_code == 412:
        payload["error"] = f"An event already exists at `{path}`. Pass a different `uid`, or omit it to generate one."
    elif response.status_code in (404, 409):
        payload["error"] = f"No calendar `{calendar}` for this caller, or it refused a new event there."
    elif not response.is_success:
        payload.update(parse_response_body(response))
    return payload


async def update_event(
    auth_context: AuthContext,
    calendar: str = None,
    path: str = None,
    uid: str = None,
    summary: str = None,
    start: str = None,
    end: str = None,
    description: str = None,
    location: str = None,
    status: str = None,
    categories: list[str] = None,
    rrule: str = None,
    organizer_email: str = None,
    organizer_name: str = None,
    attendees: list[Any] = None,
    reminders_minutes_before: list[Any] = None,
    if_match: str = None,
) -> dict[str, Any]:
    if not calendar or not path or not uid:
        raise ValueError("`calendar`, `path` and `uid` are required")
    fields = dict(
        summary=summary, start=start, end=end, description=description, location=location,
        status=status, categories=categories, rrule=rrule, organizer_email=organizer_email,
        organizer_name=organizer_name, attendees=attendees, reminders_minutes_before=reminders_minutes_before,
    )
    # A different UID would turn this into another event to every other CalDAV
    # client, so compare against what is stored before the PUT replaces it.
    existing = await get_event(auth_context, calendar, path)
    if not existing["ok"]:
        return existing
    if existing["uid"] != uid:
        raise ValueError(
            f"`uid` {uid!r} does not match the stored event's UID {existing['uid']!r}; "
            "pass the `uid` that `caldav_get_event` returned"
        )
    ics = build_vevent_ics(uid, fields, created=existing.get("created"))

    url = caldav_object_url(auth_context, calendar, path)
    extra_headers = {"Content-Type": "text/calendar; charset=utf-8"}
    if if_match:
        extra_headers["If-Match"] = normalize_etag(if_match)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.put(
            url, headers=build_webdav_headers(auth_context, extra_headers), content=ics.encode("utf-8")
        )

    payload: dict[str, Any] = {
        "ok": response.is_success, "status_code": response.status_code,
        "calendar": calendar, "path": path, "uid": uid,
        "etag": normalize_etag(response.headers.get("etag")),
    }
    if response.status_code == 412:
        payload["error"] = (
            "The event changed since `if_match` was read. Call `caldav_get_event` again and "
            "retry with the current etag."
        )
    elif response.status_code == 404:
        payload["error"] = f"No event at `{path}` in calendar `{calendar}`."
    elif not response.is_success:
        payload.update(parse_response_body(response))
    return payload


async def delete_event(
    auth_context: AuthContext, calendar: str = None, path: str = None, if_match: str = None
) -> dict[str, Any]:
    if not calendar or not path:
        raise ValueError("`calendar` and `path` are required")
    url = caldav_object_url(auth_context, calendar, path)
    extra_headers = {"If-Match": normalize_etag(if_match)} if if_match else None
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.request("DELETE", url, headers=build_webdav_headers(auth_context, extra_headers))

    payload: dict[str, Any] = {"ok": response.is_success, "status_code": response.status_code, "calendar": calendar, "path": path}
    if response.status_code == 404:
        payload["error"] = f"No event at `{path}` in calendar `{calendar}`."
    elif response.status_code == 412:
        payload["error"] = "The event changed since `if_match` was read; re-fetch it and retry if the delete should still happen."
    elif not response.is_success:
        payload.update(parse_response_body(response))
    return payload


CALENDAR_COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")


async def create_calendar(
    auth_context: AuthContext, calendar: str = None, display_name: str = None, color: str = None
) -> dict[str, Any]:
    if not calendar:
        raise ValueError("`calendar` is required")
    calendar_id = caldav_calendar_id(calendar)

    props_xml = ""
    if display_name:
        props_xml += f"<d:displayname>{xml_escape(display_name)}</d:displayname>"
    if color:
        if not CALENDAR_COLOR_PATTERN.fullmatch(color):
            raise ValueError("`color` must be a hex color like `#3388FF` or `#3388FFFF`")
        props_xml += f'<ic:calendar-color xmlns:ic="http://apple.com/ns/ical/">{color}</ic:calendar-color>'

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        f"<d:set><d:prop>{props_xml}</d:prop></d:set>"
        "</c:mkcalendar>"
    )
    url = caldav_calendar_url(auth_context, calendar_id)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            "MKCALENDAR", url,
            headers=build_webdav_headers(auth_context, {"Content-Type": "application/xml; charset=utf-8"}),
            content=body,
        )

    payload: dict[str, Any] = {"ok": response.is_success, "status_code": response.status_code, "calendar": calendar_id}
    if response.status_code == 405:
        payload["error"] = f"A calendar already exists at `{calendar_id}`."
    elif not response.is_success:
        payload.update(parse_response_body(response))
    return payload


async def delete_calendar(auth_context: AuthContext, calendar: str = None, confirm: bool = False) -> dict[str, Any]:
    if not calendar:
        raise ValueError("`calendar` is required")
    calendar_id = caldav_calendar_id(calendar)
    if not confirm:
        return {
            "ok": False, "calendar": calendar_id,
            "error": f"Deleting calendar `{calendar_id}` removes every event inside it. Pass `confirm` as true to proceed.",
        }

    url = caldav_calendar_url(auth_context, calendar_id)
    timeout = httpx2.Timeout(DISCOVERY_TIMEOUT_SECONDS)

    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.request("DELETE", url, headers=build_webdav_headers(auth_context))

    payload: dict[str, Any] = {"ok": response.is_success, "status_code": response.status_code, "calendar": calendar_id}
    if response.status_code == 404:
        payload["error"] = f"No calendar at `{calendar_id}`."
    elif not response.is_success:
        payload.update(parse_response_body(response))
    return payload


CALDAV_HANDLERS = {
    "list_calendars": list_calendars,
    "list_events": list_events,
    "get_event": get_event,
    "create_event": create_event,
    "update_event": update_event,
    "delete_event": delete_event,
    "create_calendar": create_calendar,
    "delete_calendar": delete_calendar,
}

BUILTIN_HANDLERS = {**WEBDAV_HANDLERS, **CALDAV_HANDLERS}


def builtin_operation(
    name: str,
    method: str,
    handler: str,
    summary: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    app_id: str = WEBDAV_APP_ID,
    app_name: str = "WebDAV files",
    path: str = f"{WEBDAV_ROOT}/{{user}}/{{path}}",
) -> OperationDefinition:
    return OperationDefinition(
        name=name,
        app_id=app_id,
        app_name=app_name,
        method=method,
        path=path,
        summary=summary,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        path_params=[],
        query_params=[],
        header_params=[],
        body_mode="none",
        body_fields=[],
        body_content_type=None,
        handler=handler,
    )


CALDAV_CALENDAR_PROPERTY = {"type": "string", "description": "Calendar id, as returned by `caldav_list_calendars`."}
CALDAV_EVENT_PATH_PROPERTY = {
    "type": "string",
    "description": "Event path within the calendar, exactly as returned by `caldav_list_events`, "
                   "`caldav_get_event` or `caldav_create_event` - a calendar object's filename is "
                   "not guaranteed to match its `uid`, so this must not be constructed by hand.",
}
CALDAV_START_PROPERTY = {
    "type": "string",
    "description": "`YYYY-MM-DD` for an all-day event, or a full ISO-8601 datetime with a UTC "
                   "offset or `Z`, e.g. `2026-09-20T14:00:00+08:00`. A bare local datetime with "
                   "no offset is rejected - CalDAV has no notion of the caller's timezone.",
}
CALDAV_END_PROPERTY = {
    "type": "string",
    "description": "Same format as `start`. Required for a timed event; optional for a single "
                   "all-day event, where it defaults to the same day as `start`.",
}
CALDAV_EVENT_OPTIONAL_FIELDS: dict[str, Any] = {
    "description": {"type": "string", "description": "Free-text event body."},
    "location": {"type": "string"},
    "status": {"type": "string", "enum": ["CONFIRMED", "TENTATIVE", "CANCELLED"]},
    "categories": {"type": "array", "items": {"type": "string"}},
    "rrule": {
        "type": "string",
        "description": "Raw RFC 5545 recurrence rule value, e.g. `FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10`. "
                       "Passed through as-is - not translated from natural language.",
    },
    "organizer_email": {"type": "string"},
    "organizer_name": {"type": "string"},
    "attendees": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"email": {"type": "string"}, "name": {"type": "string"}},
            "required": ["email"],
        },
    },
    "reminders_minutes_before": {
        "type": "array",
        "items": {"type": "integer", "minimum": 0},
        "description": "One popup reminder per entry, that many minutes before `start`.",
    },
}

# Hand-written because ocs_api_viewer cannot describe them: calendars are CalDAV
# (RFC 4791), which is not an OCS REST API and so is invisible to discovery
# entirely. Registering them in the catalogue rather than as their own tools
# keeps the fixed surface small and delivers each operation's warnings through
# `describe`, at the moment of use, instead of parking them in every client's
# context for the whole session.
CALDAV_OPERATIONS = [
    builtin_operation(
        "caldav_list_calendars", "PROPFIND", "list_calendars",
        "List the caller's calendars",
        "List every calendar the caller can see - id, display name, color, ctag (bumps whenever "
        "the calendar's contents change) and whether the caller can write to it. Calendars live "
        "under CalDAV (RFC 4791), which `ocs_api_viewer` does not describe at all, so this is "
        "hand-written the same way the webdav_* operations are for files.",
        {}, [],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/",
    ),
    builtin_operation(
        "caldav_list_events", "REPORT", "list_events",
        "List events in a time window",
        "List events, either from one `calendar` or across every calendar the caller can see when "
        "it is omitted. Bounded to a time window by default - `time_min`/`time_max` (each "
        "`YYYY-MM-DD` or a full ISO-8601 datetime), defaulting to now through "
        f"{DEFAULT_EVENT_WINDOW_DAYS} days out - because a CalDAV REPORT has no pagination and an "
        "unbounded query against a calendar with years of recurring events could return all of it. "
        "Pass `all_time` to lift the bound deliberately. A recurring event is returned once, as its "
        "master; `override_count` says how many dated exceptions exist without expanding them. Only "
        "VEVENT (calendar events) is covered - VTODO and VJOURNAL entries are not returned.",
        {
            "calendar": CALDAV_CALENDAR_PROPERTY,
            "time_min": {"type": "string", "description": "Start of the window. Defaults to now."},
            "time_max": {
                "type": "string",
                "description": f"End of the window. Defaults to `time_min` plus {DEFAULT_EVENT_WINDOW_DAYS} days.",
            },
            "all_time": {
                "type": "boolean",
                "description": "Ignore time_min/time_max and return every event. Can be slow or very "
                               "large on a busy calendar - prefer a bounded window.",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": EVENT_LIST_MAX_LIMIT,
                "description": f"Maximum events to return, applied after sorting by start time. "
                               f"Defaults to {EVENT_LIST_DEFAULT_LIMIT}.",
            },
        },
        [],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/",
    ),
    builtin_operation(
        "caldav_get_event", "GET", "get_event",
        "Get one event's full details",
        "Read one event - every field `caldav_list_events` summarises, plus description, "
        "attendees, reminders, the raw iCalendar text and any dated recurrence overrides. Times "
        "tagged with an IANA zone (`TZID=Asia/Taipei`, say) are returned as the wall-clock time "
        "plus that zone name rather than converted to an absolute instant - this server does not "
        "expand `VTIMEZONE` tables. Carries an `etag`, for a guarded `caldav_update_event` or "
        "`caldav_delete_event` afterwards.",
        {"calendar": CALDAV_CALENDAR_PROPERTY, "path": CALDAV_EVENT_PATH_PROPERTY},
        ["calendar", "path"],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/{{path}}",
    ),
    builtin_operation(
        "caldav_create_event", "PUT", "create_event",
        "Create a new event",
        "Create a single VEVENT in `calendar`. `uid` is generated if omitted; pass one only to "
        "control the event's identity, e.g. when importing from elsewhere. Fails rather than "
        "overwriting if that `uid` is already in use. Returns the `path` to pass to "
        "`caldav_get_event`, `caldav_update_event` or `caldav_delete_event`.",
        {
            "calendar": CALDAV_CALENDAR_PROPERTY,
            "summary": {"type": "string", "description": "Event title."},
            "start": CALDAV_START_PROPERTY,
            "end": CALDAV_END_PROPERTY,
            **CALDAV_EVENT_OPTIONAL_FIELDS,
            "uid": {"type": "string", "description": "Explicit UID. A random one is generated otherwise."},
        },
        ["calendar", "summary", "start"],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/",
    ),
    builtin_operation(
        "caldav_update_event", "PUT", "update_event",
        "Replace an existing event",
        "Replace an event's entire iCalendar content - like `webdav_write_file`, this is a full "
        "replace, not a field-level patch, so fields left out are cleared rather than preserved. "
        "Re-read the event with `caldav_get_event` first, apply the change to what it returned, and "
        "send everything back including the unchanged fields. `uid` must be the event's existing "
        "UID - changing it would make this a different event to every other CalDAV client. Pass "
        "`if_match` from a prior read to fail with 412 instead of discarding a concurrent edit.",
        {
            "calendar": CALDAV_CALENDAR_PROPERTY,
            "path": CALDAV_EVENT_PATH_PROPERTY,
            "uid": {"type": "string", "description": "The event's existing UID, from `caldav_get_event`."},
            "summary": {"type": "string"},
            "start": CALDAV_START_PROPERTY,
            "end": CALDAV_END_PROPERTY,
            **CALDAV_EVENT_OPTIONAL_FIELDS,
            "if_match": {
                "type": "string",
                "description": "ETag from a prior `caldav_get_event`. Omit only when deliberately overwriting.",
            },
        },
        ["calendar", "path", "uid", "summary", "start"],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/{{path}}",
    ),
    builtin_operation(
        "caldav_delete_event", "DELETE", "delete_event",
        "Delete an event",
        "Delete one event. Pass `if_match` from a prior read to refuse the delete if the event "
        "changed since - useful before removing something an automated pass found stale.",
        {
            "calendar": CALDAV_CALENDAR_PROPERTY,
            "path": CALDAV_EVENT_PATH_PROPERTY,
            "if_match": {"type": "string", "description": "ETag from a prior read. Optional."},
        },
        ["calendar", "path"],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/{{path}}",
    ),
    builtin_operation(
        "caldav_create_calendar", "MKCALENDAR", "create_calendar",
        "Create a new calendar",
        "Create a new calendar collection. `calendar` becomes its id (the URL segment other "
        "caldav_* operations address it by), so keep it URL-safe; `display_name` is what Nextcloud "
        "shows in its UI. `color` is best-effort - most CalDAV servers, Nextcloud included, accept "
        "it, but nothing guarantees it.",
        {
            "calendar": CALDAV_CALENDAR_PROPERTY,
            "display_name": {"type": "string"},
            "color": {"type": "string", "description": "Hex color, e.g. `#3388FF`."},
        },
        ["calendar"],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/",
    ),
    builtin_operation(
        "caldav_delete_calendar", "DELETE", "delete_calendar",
        "Delete a calendar and everything in it",
        "Delete a calendar and every event inside it. Unlike `webdav_delete`, Nextcloud's calendar "
        "trash retention is not something this server has verified, so treat this as permanent. "
        "Refused unless `confirm` is true.",
        {
            "calendar": CALDAV_CALENDAR_PROPERTY,
            "confirm": {
                "type": "boolean",
                "description": "Must be true - confirms the whole calendar should be removed.",
            },
        },
        ["calendar"],
        app_id=CALDAV_APP_ID, app_name="CalDAV calendars", path=f"{CALDAV_ROOT}/{{user}}/{{calendar}}/",
    ),
]


# Hand-written because ocs_api_viewer cannot describe them: the OCS API reports
# metadata about files but never serves their bytes, creates an ordinary folder
# or deletes anything. Registering them in the catalogue rather than as their own
# tools keeps the fixed surface small and delivers each operation's warnings
# through `describe`, at the moment of use, instead of parking them in every
# client's context for the whole session.
BUILTIN_OPERATIONS: dict[str, OperationDefinition] = {
    definition.name: definition
    for definition in [
        builtin_operation(
            "webdav_read_file", "GET", "read_file",
            "Read a file's contents",
            "Read a file from the caller's Nextcloud files over WebDAV and return its contents "
            "plus an ETag. Use this for anything the OCS API only describes rather than serves - "
            "notably the body of a Collectives page, which the collectives operations report "
            "metadata for but never return. Compose a page's path from its own index entry: "
            "`{collectivePath}/{filePath}/{fileName}`, dropping `filePath` when it is empty. "
            "Binary comes back base64-encoded in `data_base64`. A folder is refused rather than "
            "returning the placeholder page Nextcloud serves for one.",
            {"path": PATH_PROPERTY},
            ["path"],
        ),
        builtin_operation(
            "webdav_write_file", "PUT", "write_file",
            "Replace a file's contents",
            "Replace a file's contents over WebDAV. Pass `content` for text or `content_base64` "
            "for binary - feeding a base64 read back through `content` would write the base64 "
            "itself and corrupt the file. This overwrites the whole file, so read it first and "
            "send the `etag` you got back as `if_match`: the write then fails with 412 instead of "
            "discarding an edit somebody made in between. The parent folder must already exist - "
            "a write below a missing one answers 404, not the 409 the WebDAV spec implies - so "
            "create it with `webdav_create_folder`, or for a Collectives page use "
            "`collectives_page_create` and write the body here.",
            {
                "path": PATH_PROPERTY,
                "content": {"type": "string", "description": "The complete new contents, as text."},
                "content_base64": {
                    "type": "string",
                    "description": "The complete new contents, base64-encoded, for binary files. "
                                   "Mutually exclusive with `content`.",
                },
                "if_match": {
                    "type": "string",
                    "description": "ETag from a prior `webdav_read_file`. Omit only when "
                                   "deliberately overwriting whatever is there.",
                },
            },
            ["path"],
        ),
        builtin_operation(
            "webdav_list_directory", "PROPFIND", "list_directory",
            "List a folder's contents",
            "List a folder's immediate children over WebDAV - name, path, whether it is a folder, "
            "size, content type, last modified and ETag. Unlike `files_api_get_folder_tree`, which "
            "reports folders only, this includes files. Paths come back ready to pass to the other "
            "file operations.",
            {
                "path": {
                    "type": "string",
                    "description": "Folder to list. Omit or pass an empty string for the files root.",
                }
            },
            [],
        ),
        builtin_operation(
            "webdav_create_folder", "MKCOL", "create_folder",
            "Create a folder",
            "Create a folder over WebDAV. By default every missing folder on the way is created "
            "too, which is what makes uploading a directory tree possible at all - MKCOL itself "
            "makes exactly one level. A folder that already exists is reported as success, so this "
            "is safe to call repeatedly while walking a tree.",
            {
                "path": PATH_PROPERTY,
                "parents": {
                    "type": "boolean",
                    "description": "Create missing intermediate folders as well. Defaults to true; "
                                   "set false to fail unless the parent already exists.",
                },
            },
            ["path"],
        ),
        builtin_operation(
            "webdav_delete", "DELETE", "delete_path",
            "Delete a file or folder",
            "Delete a file, or a folder and everything inside it, over WebDAV. Deleting a folder is "
            "always recursive - WebDAV has no shallow variant - so a folder is refused unless "
            "`recursive` is true. Deleted items go to the Nextcloud trash bin, not straight to "
            "oblivion.",
            {
                "path": PATH_PROPERTY,
                "recursive": {
                    "type": "boolean",
                    "description": "Required to be true when the path is a folder, confirming that "
                                   "everything inside it should go too.",
                },
            },
            ["path"],
        ),
    ]
    + CALDAV_OPERATIONS
}


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
    authenticated = execution_auth_context(ctx).auth_header is not None
    return types.ListToolsResult(tools=list_tools(authenticated))


def missing_credentials_result(tool_name: str) -> types.CallToolResult:
    return tool_result(
        {
            "ok": False,
            "error": (
                "Missing request credentials for this tool call. Configure MCP HTTP headers "
                "`X-Nextcloud-Username` and `X-Nextcloud-AppToken`. "
                "Server discovery credentials are not used for tool execution over HTTP."
            ),
            "tool_name": tool_name,
        },
        is_error=True,
    )


def find_result(arguments: dict[str, Any], auth_context: AuthContext) -> types.CallToolResult:
    """Searching the catalogue is gated as tightly as executing against it.

    The catalogue names every app the instance has installed and every API path
    it exposes. Leaving search open let anyone who knew the endpoint URL
    enumerate that, which is the reconnaissance half of an attack even though
    execution itself stayed credentialed.
    """
    if not auth_context.auth_header:
        return missing_credentials_result(FIND_TOOL_NAME)

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


def describe_result(arguments: dict[str, Any], auth_context: AuthContext) -> types.CallToolResult:
    if not auth_context.auth_header:
        return missing_credentials_result(DESCRIBE_TOOL_NAME)

    names = arguments.get("names") or []
    if isinstance(names, str):
        names = [names]

    operations = all_operations()
    described: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    for name in names:
        definition = operations.get(name)
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
    # Credentials are checked before the lookup: `did_you_mean` is built from
    # the catalogue, so answering an unknown name first would let an anonymous
    # caller enumerate operations one guess at a time.
    if not auth_context.auth_header:
        return missing_credentials_result(CALL_TOOL_NAME)

    operation_name = arguments.get("name")
    definition = all_operations().get(operation_name)
    if definition is None:
        return tool_result(
            {
                "ok": False,
                "error": f"Unknown operation: {operation_name}",
                "did_you_mean": suggest_operation_names(operation_name),
            },
            is_error=True,
        )

    operation_arguments = arguments.get("arguments") or {}
    try:
        if definition.handler:
            # `arguments` is passed through unvalidated, so trim it to the schema
            # rather than letting a stray key become an unexpected keyword.
            accepted = set(definition.input_schema.get("properties", {}))
            payload = await BUILTIN_HANDLERS[definition.handler](
                auth_context,
                **{key: value for key, value in operation_arguments.items() if key in accepted},
            )
        else:
            payload = await execute_operation(definition, operation_arguments, auth_context)
    except ValueError as exc:
        return tool_result({"ok": False, "error": str(exc), "tool_name": definition.name}, is_error=True)
    payload.setdefault("operation", definition.name)
    return tool_result(payload, is_error=not payload.get("ok", True))


async def on_call_tool(
    ctx: ServerRequestContext[Any, Any],
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    arguments = params.arguments or {}
    auth_context = execution_auth_context(ctx)

    if params.name == STATUS_TOOL_NAME:
        # A forced refresh makes the server re-fetch every app's OpenAPI document
        # with its own discovery credentials. Leaving that open to anonymous
        # callers turns one unauthenticated request into ~29 upstream ones.
        if arguments.get("refresh"):
            if not auth_context.auth_header:
                return missing_credentials_result(STATUS_TOOL_NAME)
            await refresh_state(force=True)
        return tool_result(discovery_status_payload(auth_context))

    await refresh_state()

    if params.name == FIND_TOOL_NAME:
        return find_result(arguments, auth_context)
    if params.name == DESCRIBE_TOOL_NAME:
        return describe_result(arguments, auth_context)
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
