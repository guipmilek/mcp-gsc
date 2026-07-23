"""Public direct CRUD tools for the Horizon Search Console server."""

from __future__ import annotations

from typing import Any

from . import google_api
from .config import (
    CRUD_CONTRACT_VERSION,
    OPERATION_HASH_VERSION,
    RESOURCE_REGISTRY,
    GscSafetyError,
    assert_allowed_site,
    assert_allowed_sitemap,
    canonical_site_url,
    canonical_sitemap_url,
    load_scope_config,
)
from .coordinator import coordinate


async def gsc_crud_status() -> dict[str, Any]:
    """Return the direct CRUD contract and configured non-secret scope."""

    config = load_scope_config()
    return {
        "contract_version": CRUD_CONTRACT_VERSION,
        "runtime": "PYTHON_FASTMCP_HORIZON",
        "write_mode": "DIRECT",
        "deployment_env_keys": ["MCP_CREDENTIALS", "MCP_CONFIG"],
        "optional_deployment_env_keys": ["MCP_CONFIG"],
        "empty_allowlist_behavior": "ALLOW_ALL_ACCESSIBLE",
        "dry_run_supported": True,
        "approval_workflow": False,
        "allowlists": {
            "site_urls": list(config.allowed_site_urls),
            "sitemap_prefixes": list(config.allowed_sitemap_prefixes),
        },
        "max_operations_per_request": config.max_operations_per_request,
        "atomic": False,
        "execution_strategy": "SEQUENTIAL_STOP_ON_FIRST_ERROR",
        "native_google_api_validation": False,
        "operation_hash_version": OPERATION_HASH_VERSION,
    }


async def gsc_list_mutable_resources() -> dict[str, Any]:
    """List the exact direct CRUD facade and API-supported actions."""

    return {
        "contract_version": CRUD_CONTRACT_VERSION,
        "resources": [
            {
                "resource": resource,
                "actions": details["actions"],
                "create_action": details["create_action"],
                "delete_action": details["delete_action"],
                "update_supported": details["update_supported"],
            }
            for resource, details in RESOURCE_REGISTRY.items()
        ],
        "unsupported_actions": ["update", "archive", "restore"],
        "public_write_tools": [
            "gsc_add_site",
            "gsc_delete_site",
            "gsc_submit_sitemap",
            "gsc_delete_sitemap",
            "gsc_batch_operations",
        ],
        "write_mode": "DIRECT",
        "dry_run_supported": True,
    }


async def gsc_get_mutation_schema(resource: str, action: str) -> dict[str, Any]:
    """Return the direct input schema for one GSC resource action."""

    resource, action = resource.strip(), action.strip().lower()
    if resource not in RESOURCE_REGISTRY:
        raise GscSafetyError(
            "UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}."
        )
    if action not in RESOURCE_REGISTRY[resource]["actions"]:
        raise GscSafetyError(
            "UNSUPPORTED_ACTION",
            f"Unsupported {resource} action: {action!r}.",
        )
    schemas = {
        ("Site", "add"): {"required": ["site_url"], "optional": ["dry_run"]},
        ("Site", "delete"): {
            "required": ["site_url"],
            "optional": ["dry_run"],
        },
        ("Site", "get"): {"required": ["site_url"]},
        ("Site", "list"): {"required": []},
        ("Sitemap", "submit"): {
            "required": ["site_url", "sitemap_url"],
            "optional": ["dry_run"],
        },
        ("Sitemap", "delete"): {
            "required": ["site_url", "sitemap_url"],
            "optional": ["dry_run"],
        },
        ("Sitemap", "get"): {"required": ["site_url", "resource_name"]},
        ("Sitemap", "list"): {"required": ["site_url"]},
    }
    tool_names = {
        ("Site", "add"): "gsc_add_site",
        ("Site", "delete"): "gsc_delete_site",
        ("Sitemap", "submit"): "gsc_submit_sitemap",
        ("Sitemap", "delete"): "gsc_delete_sitemap",
    }
    return {
        "contract_version": CRUD_CONTRACT_VERSION,
        "resource": resource,
        "action": action,
        "schema": schemas[(resource, action)],
        "tool": tool_names.get((resource, action)),
        "write_mode": (
            "DIRECT" if (resource, action) in tool_names else "READ_ONLY"
        ),
        "approval_required": False,
    }


async def gsc_get_resource(
    resource: str, site_url: str, resource_name: str | None = None
) -> dict[str, Any]:
    """Read one allowlisted Site or Sitemap."""

    config = load_scope_config()
    canonical_site = canonical_site_url(site_url)
    assert_allowed_site(config, canonical_site)
    client = google_api.service()
    if resource == "Site":
        return {
            "contract_version": CRUD_CONTRACT_VERSION,
            "resource": "Site",
            "value": google_api.read_site(
                client, canonical_site, allow_missing=False
            ),
        }
    if resource == "Sitemap":
        sitemap_url = canonical_sitemap_url(resource_name or "")
        assert_allowed_sitemap(config, sitemap_url)
        return {
            "contract_version": CRUD_CONTRACT_VERSION,
            "resource": "Sitemap",
            "value": google_api.read_sitemap(
                client,
                canonical_site,
                sitemap_url,
                allow_missing=False,
            ),
        }
    raise GscSafetyError(
        "UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}."
    )


async def gsc_list_resources(
    resource: str, site_url: str | None = None
) -> dict[str, Any]:
    """List allowlisted Sites or Sitemaps."""

    config = load_scope_config()
    client = google_api.service()
    if resource == "Site":
        allowed = set(config.allowed_site_urls)
        values = [
            item
            for item in google_api.list_sites(client)
            if item["site_url"] in allowed
        ]
        return {
            "contract_version": CRUD_CONTRACT_VERSION,
            "resource": "Site",
            "count": len(values),
            "values": values,
        }
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
        return {
            "contract_version": CRUD_CONTRACT_VERSION,
            "resource": "Sitemap",
            "count": len(values),
            "values": values,
        }
    raise GscSafetyError(
        "UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}."
    )


async def gsc_add_site(site_url: str, dry_run: bool = False) -> dict[str, Any]:
    """Add an allowlisted Search Console Site directly and idempotently."""

    return coordinate(
        [{"action": "add", "resource": "Site", "site_url": site_url}],
        dry_run=dry_run,
    )


async def gsc_delete_site(
    site_url: str, dry_run: bool = False
) -> dict[str, Any]:
    """Remove an allowlisted Site directly; an absent Site is successful."""

    return coordinate(
        [{"action": "delete", "resource": "Site", "site_url": site_url}],
        dry_run=dry_run,
    )


async def gsc_submit_sitemap(
    site_url: str,
    sitemap_url: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Submit an allowlisted Sitemap directly and idempotently."""

    return coordinate(
        [
            {
                "action": "submit",
                "resource": "Sitemap",
                "site_url": site_url,
                "data": {"sitemap_url": sitemap_url},
            }
        ],
        dry_run=dry_run,
    )


async def gsc_delete_sitemap(
    site_url: str,
    sitemap_url: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove an allowlisted Sitemap directly; absence is successful."""

    return coordinate(
        [
            {
                "action": "delete",
                "resource": "Sitemap",
                "site_url": site_url,
                "resource_name": sitemap_url,
            }
        ],
        dry_run=dry_run,
    )


async def gsc_batch_operations(
    operations: list[dict[str, Any]], dry_run: bool = False
) -> dict[str, Any]:
    """Run a direct non-atomic mutation batch, stopping on first failure."""

    return coordinate(operations, dry_run=dry_run)
