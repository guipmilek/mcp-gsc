"""Public MCP tools for protected GSC reads and mutations."""

from __future__ import annotations

from typing import Any

from . import google_api
from .config import (
    CONFIRMATION_TOKEN_VERSION,
    MINIMUM_SECRET_BYTES,
    OPERATION_HASH_VERSION,
    RESOURCE_REGISTRY,
    GscSafetyError,
    assert_allowed_site,
    assert_allowed_sitemap,
    canonical_site_url,
    canonical_sitemap_url,
    load_safety_config,
)
from .confirmations import diagnostics
from .coordinator import coordinate


async def gsc_safety_status() -> dict[str, Any]:
    """Return non-secret local safety state without calling Google APIs."""

    config = load_safety_config()
    return {
        "runtime": "PYTHON_FASTMCP_HORIZON",
        "mutations_enabled": config.mutations_enabled,
        "gates": {
            "site_add": config.allow_site_add,
            "site_delete": config.allow_site_delete,
            "sitemap_submit": config.allow_sitemap_submit,
            "sitemap_delete": config.allow_sitemap_delete,
        },
        "allowlists": {
            "site_urls": list(config.allowed_site_urls),
            "sitemap_prefixes": list(config.allowed_sitemap_prefixes),
        },
        "max_operations_per_request": config.max_operations_per_request,
        "confirmation_ttl_seconds": config.confirmation_ttl_seconds,
        "confirmation_secret_configured": config.confirmation_secret is not None,
        "confirmation_secret_minimum_bytes": MINIMUM_SECRET_BYTES,
        "operation_hash_version": OPERATION_HASH_VERSION,
        "confirmation_token_version": CONFIRMATION_TOKEN_VERSION,
        "replay_protection": "BEST_EFFORT_PROCESS_LOCAL",
        "globally_single_use": False,
        "atomic": False,
        "execution_strategy": "SEQUENTIAL_STOP_ON_FIRST_ERROR",
        "admin_api_validate_only_supported": False,
        "validation_kind": "CONNECTOR_PREFLIGHT",
    }


async def gsc_confirmation_diagnostics() -> dict[str, Any]:
    """Verify local confirmation signing without exposing or consuming a token."""

    return diagnostics(load_safety_config())


async def gsc_list_mutable_resources() -> dict[str, Any]:
    """List the exact mutable facade and unsupported pseudo-CRUD actions."""

    return {
        "resources": [
            {
                "resource": resource,
                "actions": details["actions"],
                "create_action": details["create_action"],
                "delete_action": details["delete_action"],
                "risk_gates": details["risk_gates"],
            }
            for resource, details in RESOURCE_REGISTRY.items()
        ],
        "unsupported_actions": ["update", "archive", "restore"],
        "horizon_public_mutation_tools": [
            "gsc_prepare_site_add",
            "gsc_execute_site_add",
            "gsc_prepare_site_delete",
            "gsc_execute_site_delete",
            "gsc_prepare_sitemap_submit",
            "gsc_execute_sitemap_submit",
            "gsc_prepare_sitemap_delete",
            "gsc_execute_sitemap_delete",
        ],
        "generic_internal_facade_exposed_by_horizon": False,
    }


async def gsc_get_mutation_schema(resource: str, action: str) -> dict[str, Any]:
    """Return the current facade schema for one resource and action."""

    resource, action = resource.strip(), action.strip().lower()
    if resource not in RESOURCE_REGISTRY:
        raise GscSafetyError("UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}.")
    if action not in RESOURCE_REGISTRY[resource]["actions"]:
        raise GscSafetyError("UNSUPPORTED_ACTION", f"Unsupported {resource} action: {action!r}.")
    schemas = {
        ("Site", "add"): {"required": ["site_url"], "data": None},
        ("Site", "delete"): {"required": ["site_url"], "data": None},
        ("Site", "get"): {"required": ["site_url"]},
        ("Site", "list"): {"required": []},
        ("Sitemap", "submit"): {"required": ["site_url", "sitemap_url"]},
        ("Sitemap", "delete"): {"required": ["site_url", "sitemap_url"]},
        ("Sitemap", "get"): {"required": ["site_url", "resource_name"]},
        ("Sitemap", "list"): {"required": ["site_url"]},
    }
    result = {
        "resource": resource,
        "action": action,
        "schema": schemas[(resource, action)],
    }
    if action in {"add", "submit", "delete"}:
        tool_suffix = f"{resource.lower()}_{action}"
        result["workflow"] = {
            "prepare_tool": f"gsc_prepare_{tool_suffix}",
            "execute_tool": f"gsc_execute_{tool_suffix}",
            "prepare_is_read_only": True,
            "execute_requires_confirmation": True,
            "validation_kind": "CONNECTOR_PREFLIGHT",
            "native_google_api_validation": False,
        }
    return result


async def gsc_get_resource(
    resource: str, site_url: str, resource_name: str | None = None
) -> dict[str, Any]:
    """Read one allowlisted Site or Sitemap."""

    config = load_safety_config()
    canonical_site = canonical_site_url(site_url)
    assert_allowed_site(config, canonical_site)
    client = google_api.service()
    if resource == "Site":
        return {
            "resource": "Site",
            "value": google_api.read_site(client, canonical_site, allow_missing=False),
        }
    if resource == "Sitemap":
        sitemap_url = canonical_sitemap_url(resource_name or "")
        assert_allowed_sitemap(config, sitemap_url)
        return {
            "resource": "Sitemap",
            "value": google_api.read_sitemap(
                client, canonical_site, sitemap_url, allow_missing=False
            ),
        }
    raise GscSafetyError("UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}.")


async def gsc_list_resources(resource: str, site_url: str | None = None) -> dict[str, Any]:
    """List allowlisted Sites or Sitemaps."""

    config = load_safety_config()
    client = google_api.service()
    if resource == "Site":
        allowed = set(config.allowed_site_urls)
        values = [item for item in google_api.list_sites(client) if item["site_url"] in allowed]
        return {"resource": "Site", "count": len(values), "values": values}
    if resource == "Sitemap":
        canonical_site = canonical_site_url(site_url or "")
        assert_allowed_site(config, canonical_site)
        values = [
            item
            for item in google_api.list_sitemaps(client, canonical_site)
            if item["sitemap_url"]
            and any(
                item["sitemap_url"].startswith(prefix)
                for prefix in config.allowed_sitemap_prefixes
            )
        ]
        return {"resource": "Sitemap", "count": len(values), "values": values}
    raise GscSafetyError("UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}.")


def _require_confirmation(confirmation: str) -> str:
    value = str(confirmation or "").strip()
    if not value:
        raise GscSafetyError(
            "CONFIRMATION_REQUIRED",
            "An exact confirmation receipt from the matching prepare tool is required.",
        )
    return value


async def gsc_prepare_site_add(site_url: str) -> dict[str, Any]:
    """Prepare Site.add and issue a confirmation receipt without mutating Google."""

    return await gsc_create_resource("Site", site_url, validate_only=True)


async def gsc_execute_site_add(site_url: str, confirmation: str) -> dict[str, Any]:
    """Execute Site.add using the exact receipt from gsc_prepare_site_add."""

    return await gsc_create_resource(
        "Site",
        site_url,
        validate_only=False,
        confirmation=_require_confirmation(confirmation),
    )


async def gsc_prepare_site_delete(site_url: str) -> dict[str, Any]:
    """Prepare Site.delete and issue a confirmation receipt without mutating Google."""

    return await gsc_delete_resource("Site", site_url, validate_only=True)


async def gsc_execute_site_delete(site_url: str, confirmation: str) -> dict[str, Any]:
    """Execute Site.delete using the exact receipt from gsc_prepare_site_delete."""

    return await gsc_delete_resource(
        "Site",
        site_url,
        validate_only=False,
        confirmation=_require_confirmation(confirmation),
    )


async def gsc_prepare_sitemap_submit(site_url: str, sitemap_url: str) -> dict[str, Any]:
    """Prepare Sitemap.submit and issue a confirmation receipt without mutating Google."""

    return await gsc_create_resource(
        "Sitemap",
        site_url,
        data={"sitemap_url": sitemap_url},
        validate_only=True,
    )


async def gsc_execute_sitemap_submit(
    site_url: str, sitemap_url: str, confirmation: str
) -> dict[str, Any]:
    """Execute Sitemap.submit using the exact receipt from the matching prepare tool."""

    return await gsc_create_resource(
        "Sitemap",
        site_url,
        data={"sitemap_url": sitemap_url},
        validate_only=False,
        confirmation=_require_confirmation(confirmation),
    )


async def gsc_prepare_sitemap_delete(site_url: str, sitemap_url: str) -> dict[str, Any]:
    """Prepare Sitemap.delete and issue a confirmation receipt without mutating Google."""

    return await gsc_delete_resource(
        "Sitemap",
        site_url,
        resource_name=sitemap_url,
        validate_only=True,
    )


async def gsc_execute_sitemap_delete(
    site_url: str, sitemap_url: str, confirmation: str
) -> dict[str, Any]:
    """Execute Sitemap.delete using the exact receipt from the matching prepare tool."""

    return await gsc_delete_resource(
        "Sitemap",
        site_url,
        resource_name=sitemap_url,
        validate_only=False,
        confirmation=_require_confirmation(confirmation),
    )


async def gsc_create_resource(
    resource: str,
    site_url: str,
    data: dict[str, Any] | None = None,
    validate_only: bool = True,
    confirmation: str | None = None,
) -> dict[str, Any]:
    """Internal generic facade for Site.add / Sitemap.submit."""

    resource = resource.strip()
    if resource not in RESOURCE_REGISTRY:
        raise GscSafetyError("UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}.")
    return coordinate(
        [
            {
                "action": RESOURCE_REGISTRY[resource]["create_action"],
                "resource": resource,
                "site_url": site_url,
                "data": data or {},
            }
        ],
        validate_only=validate_only,
        confirmation=confirmation,
    )


async def gsc_delete_resource(
    resource: str,
    site_url: str,
    resource_name: str | None = None,
    validate_only: bool = True,
    confirmation: str | None = None,
) -> dict[str, Any]:
    """Internal generic facade for Site.delete / Sitemap.delete."""

    return coordinate(
        [
            {
                "action": "delete",
                "resource": resource,
                "site_url": site_url,
                "resource_name": resource_name or site_url,
            }
        ],
        validate_only=validate_only,
        confirmation=confirmation,
    )


async def gsc_batch_operations(
    operations: list[dict[str, Any]],
    validate_only: bool = True,
    confirmation: str | None = None,
) -> dict[str, Any]:
    """Internal generic batch facade; not exposed by the Horizon server."""

    return coordinate(operations, validate_only=validate_only, confirmation=confirmation)
