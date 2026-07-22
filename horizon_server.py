"""FastMCP/Horizon entrypoint with protected Google Search Console writes."""

from __future__ import annotations

import base64
import inspect
import json
import os
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable


def _configure_credentials_from_base64() -> Path | None:
    encoded = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64", "").strip()
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64 is not valid base64 JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Decoded Google credentials must be a JSON object.")

    path = Path(os.getenv("GSC_ADC_PATH", "/tmp/google-search-console-adc.json"))
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


_configure_credentials_from_base64()

from fastmcp import FastMCP  # noqa: E402
from fastmcp.tools import Tool  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import gsc_server as legacy  # noqa: E402
from gsc_safe import (  # noqa: E402
    GscSafetyError,
    gsc_batch_operations,
    gsc_confirmation_diagnostics,
    gsc_create_resource,
    gsc_delete_resource,
    gsc_get_mutation_schema,
    gsc_get_resource,
    gsc_list_mutable_resources,
    gsc_list_resources,
    gsc_safety_status,
)

ToolFunction = Callable[..., Awaitable[Any]]

_READ_TOOLS: tuple[ToolFunction, ...] = tuple(
    function
    for function in (
        getattr(legacy, "list_properties", None),
        getattr(legacy, "get_site_details", None),
        getattr(legacy, "get_search_analytics", None),
        getattr(legacy, "get_performance_overview", None),
        getattr(legacy, "compare_search_periods", None),
        getattr(legacy, "get_search_by_page_query", None),
        getattr(legacy, "get_advanced_search_analytics", None),
        getattr(legacy, "inspect_url_enhanced", None),
        getattr(legacy, "batch_url_inspection", None),
        getattr(legacy, "check_indexing_issues", None),
        getattr(legacy, "get_sitemaps", None),
        getattr(legacy, "list_sitemaps_enhanced", None),
        getattr(legacy, "get_sitemap_details", None),
        gsc_safety_status,
        gsc_confirmation_diagnostics,
        gsc_list_mutable_resources,
        gsc_get_mutation_schema,
        gsc_get_resource,
        gsc_list_resources,
    )
    if function is not None
)

_MUTATION_TOOLS: tuple[ToolFunction, ...] = (
    gsc_create_resource,
    gsc_delete_resource,
    gsc_batch_operations,
)


def _with_structured_errors(function: ToolFunction) -> ToolFunction:
    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await function(*args, **kwargs)
        except GscSafetyError as exc:
            return {"error": {"type": type(exc).__name__, **exc.as_dict()}}
        except Exception as exc:
            return {"error": {"type": type(exc).__name__, "message": str(exc)}}

    wrapped.__signature__ = inspect.signature(function)  # type: ignore[attr-defined]
    return wrapped


def _add_tool(server: FastMCP, function: ToolFunction, *, read_only: bool) -> None:
    server.add_tool(
        Tool.from_function(
            _with_structured_errors(function),
            annotations=ToolAnnotations(readOnlyHint=read_only),
        )
    )


def create_horizon_server() -> FastMCP:
    server = FastMCP("Google Search Console MCP Server")
    for function in _READ_TOOLS:
        _add_tool(server, function, read_only=True)
    for function in _MUTATION_TOOLS:
        _add_tool(server, function, read_only=False)
    return server


mcp = create_horizon_server()
