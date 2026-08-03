"""FastMCP/Horizon entrypoint with direct Google Search Console CRUD."""

from __future__ import annotations

import base64
import inspect
import json
import os
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

_ADC_PATH = Path("/tmp/google-search-console-adc.json")


def _configure_deployment_credentials() -> Path | None:
    """Materialize Google ADC from an envelope or raw credential object."""

    encoded = os.getenv("MCP_CREDENTIALS", "").strip()
    if not encoded:
        return None
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "MCP_CREDENTIALS must be a base64-encoded JSON object."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MCP_CREDENTIALS must decode to a JSON object.")
    credentials = payload.get("google_credentials")
    if credentials is None and isinstance(payload.get("type"), str):
        credentials = payload
    if credentials is None:
        return None
    if not isinstance(credentials, dict):
        raise RuntimeError("MCP_CREDENTIALS.google_credentials must be a JSON object.")

    raw = json.dumps(credentials, separators=(",", ":")).encode("utf-8")
    path = _ADC_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    os.environ["GSC_CREDENTIALS_PATH"] = str(path)
    os.environ.setdefault("GSC_SKIP_OAUTH", "true")
    return path


_configure_deployment_credentials()

from fastmcp import FastMCP  # noqa: E402
from fastmcp.tools import Tool  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import gsc_server as legacy  # noqa: E402
from gsc_safe import (  # noqa: E402
    GscSafetyError,
    gsc_add_site,
    gsc_batch_operations,
    gsc_crud_status,
    gsc_delete_site,
    gsc_delete_sitemap,
    gsc_get_mutation_schema,
    gsc_get_resource,
    gsc_list_mutable_resources,
    gsc_list_resources,
    gsc_submit_sitemap,
)

ToolFunction = Callable[..., Awaitable[Any]]
ToolDefinition = tuple[ToolFunction, str]

_READ_TOOLS: tuple[ToolDefinition, ...] = tuple(
    (function, title)
    for function, title in (
        (
            getattr(legacy, "list_properties", None),
            "List Search Console Properties",
        ),
        (
            getattr(legacy, "get_site_details", None),
            "Get Search Console Site Details",
        ),
        (getattr(legacy, "get_search_analytics", None), "Get Search Analytics"),
        (
            getattr(legacy, "get_performance_overview", None),
            "Get Performance Overview",
        ),
        (
            getattr(legacy, "compare_search_periods", None),
            "Compare Search Periods",
        ),
        (
            getattr(legacy, "get_search_by_page_query", None),
            "Get Search by Page and Query",
        ),
        (
            getattr(legacy, "get_advanced_search_analytics", None),
            "Get Advanced Search Analytics",
        ),
        (getattr(legacy, "inspect_url_enhanced", None), "Inspect URL"),
        (getattr(legacy, "batch_url_inspection", None), "Batch URL Inspection"),
        (
            getattr(legacy, "check_indexing_issues", None),
            "Check Indexing Issues",
        ),
        (getattr(legacy, "get_sitemaps", None), "Get Sitemaps"),
        (
            getattr(legacy, "list_sitemaps_enhanced", None),
            "List Sitemaps Enhanced",
        ),
        (getattr(legacy, "get_sitemap_details", None), "Get Sitemap Details"),
        (gsc_crud_status, "Get GSC Direct CRUD Status"),
        (gsc_list_mutable_resources, "List GSC Mutable Resources"),
        (gsc_get_mutation_schema, "Get GSC Mutation Schema"),
        (gsc_get_resource, "Get GSC Resource"),
        (gsc_list_resources, "List GSC Resources"),
    )
    if function is not None
)

_ADDITIVE_WRITE_TOOLS: tuple[ToolDefinition, ...] = (
    (gsc_add_site, "Add Search Console Site"),
    (gsc_submit_sitemap, "Submit Search Console Sitemap"),
)

_DESTRUCTIVE_WRITE_TOOLS: tuple[ToolDefinition, ...] = (
    (gsc_delete_site, "Delete Search Console Site"),
    (gsc_delete_sitemap, "Delete Search Console Sitemap"),
    (gsc_batch_operations, "Run Search Console CRUD Batch"),
)


def _with_structured_errors(function: ToolFunction) -> ToolFunction:
    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await function(*args, **kwargs)
            if isinstance(result, str):
                try:
                    decoded = json.loads(result)
                except json.JSONDecodeError:
                    return result
                if isinstance(decoded, (dict, list)):
                    return decoded
            return result
        except GscSafetyError as exc:
            return {"error": {"type": type(exc).__name__, **exc.as_dict()}}
        except Exception as exc:
            return {"error": {"type": type(exc).__name__, "message": str(exc)}}

    wrapped.__signature__ = inspect.signature(function)  # type: ignore[attr-defined]
    return wrapped


def _tool_annotations(
    title: str,
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _add_tool(
    server: FastMCP,
    function: ToolFunction,
    title: str,
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> None:
    server.add_tool(
        Tool.from_function(
            _with_structured_errors(function),
            annotations=_tool_annotations(
                title,
                read_only=read_only,
                destructive=destructive,
                idempotent=idempotent,
                open_world=open_world,
            ),
        )
    )


def create_horizon_server() -> FastMCP:
    server = FastMCP("Google Search Console MCP Server")
    for function, title in _READ_TOOLS:
        _add_tool(
            server,
            function,
            title,
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=True,
        )
    for function, title in _ADDITIVE_WRITE_TOOLS:
        _add_tool(
            server,
            function,
            title,
            read_only=False,
            destructive=False,
            idempotent=True,
            open_world=True,
        )
    for function, title in _DESTRUCTIVE_WRITE_TOOLS:
        _add_tool(
            server,
            function,
            title,
            read_only=False,
            destructive=True,
            idempotent=True,
            open_world=True,
        )
    return server


mcp = create_horizon_server()
