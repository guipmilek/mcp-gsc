"""Validate, execute, and verify direct GSC CRUD operations."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from . import google_api
from .config import (
    CRUD_CONTRACT_VERSION,
    OPERATION_HASH_VERSION,
    RESOURCE_REGISTRY,
    GscSafetyError,
    ScopeConfig,
    assert_allowed_site,
    assert_allowed_sitemap,
    canonical_json,
    canonical_site_url,
    canonical_sitemap_url,
    load_scope_config,
    sha256_json,
)


def normalize_operation(
    raw: Mapping[str, Any], config: ScopeConfig
) -> dict[str, Any]:
    action = str(raw.get("action") or "").strip().lower()
    resource = str(raw.get("resource") or "").strip()
    if resource not in RESOURCE_REGISTRY:
        raise GscSafetyError(
            "UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}."
        )
    if action not in RESOURCE_REGISTRY[resource]["actions"]:
        raise GscSafetyError(
            "UNSUPPORTED_ACTION",
            f"Action {action!r} is not supported for {resource}.",
        )
    if action in {"get", "list"}:
        raise GscSafetyError(
            "INVALID_ARGUMENT", "Read actions are not valid batch mutations."
        )

    site_url = canonical_site_url(
        str(raw.get("site_url") or raw.get("resource_name") or "")
    )
    assert_allowed_site(config, site_url)
    operation: dict[str, Any] = {
        "action": action,
        "resource": resource,
        "site_url": site_url,
        "resource_name": site_url,
        "data": {},
    }

    if resource == "Sitemap":
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        sitemap_url = canonical_sitemap_url(
            str(raw.get("resource_name") or data.get("sitemap_url") or "")
        )
        assert_allowed_sitemap(config, sitemap_url)
        operation["resource_name"] = sitemap_url
        operation["data"] = {"sitemap_url": sitemap_url}
    elif raw.get("data") not in (None, {}):
        raise GscSafetyError(
            "INVALID_ARGUMENT", "Site operations do not accept data fields."
        )
    return operation


def _no_op_reason(operation: Mapping[str, Any], state: Any) -> str | None:
    if operation["resource"] == "Site" and operation["action"] == "add":
        return "ALREADY_PRESENT" if state is not None else None
    if operation["action"] == "delete":
        return "ALREADY_ABSENT" if state is None else None
    return None


def prepare_operations(
    raw_operations: Iterable[Mapping[str, Any]],
    config: ScopeConfig,
    client: Any,
) -> list[dict[str, Any]]:
    raw_list = list(raw_operations)
    if not raw_list:
        raise GscSafetyError(
            "INVALID_ARGUMENT", "At least one operation is required."
        )
    if len(raw_list) > config.max_operations_per_request:
        raise GscSafetyError(
            "TOO_MANY_OPERATIONS",
            "The request exceeds MCP_CONFIG.max_operations.",
            {
                "requested": len(raw_list),
                "maximum": config.max_operations_per_request,
            },
        )

    operations = [normalize_operation(raw, config) for raw in raw_list]
    for operation in operations:
        state = google_api.precondition_state(client, operation)
        operation["precondition_state"] = state
        operation["precondition_hash"] = sha256_json(state)
        operation["no_op_reason"] = _no_op_reason(operation, state)
    return operations


def operation_hash(operations: list[dict[str, Any]]) -> str:
    value = {
        "operation_hash_version": OPERATION_HASH_VERSION,
        "operations": [
            {
                "action": item["action"],
                "resource": item["resource"],
                "site_url": item["site_url"],
                "resource_name": item["resource_name"],
                "data": item["data"],
            }
            for item in operations
        ],
    }
    return hashlib.sha256(canonical_json(value)).hexdigest()[:32]


def public_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "action": item["action"],
            "resource": item["resource"],
            "site_url": item["site_url"],
            "resource_name": item["resource_name"],
            "data": item["data"],
            "precondition_hash": item["precondition_hash"],
            "no_op_reason": item["no_op_reason"],
        }
        for item in operations
    ]


def operation_scope(operations: list[dict[str, Any]]) -> dict[str, Any]:
    actions: dict[str, int] = {}
    resources: dict[str, int] = {}
    for item in operations:
        actions[item["action"]] = actions.get(item["action"], 0) + 1
        resources[item["resource"]] = resources.get(item["resource"], 0) + 1
    return {
        "actions": actions,
        "resources": resources,
        "site_urls": sorted({item["site_url"] for item in operations}),
        "requested_resource_names": [
            item["resource_name"] for item in operations
        ],
    }


def _failure(exc: Exception, operation_index: int) -> dict[str, Any]:
    status = google_api.http_status(exc)
    retryable = status is None or status == 429 or status >= 500
    return {
        "operation_index": operation_index,
        "code": getattr(exc, "code", type(exc).__name__),
        "message": str(exc),
        "details": {"http_status": status},
        "retryable": retryable,
        "execution_may_have_completed": retryable,
    }


def coordinate(
    raw_operations: Iterable[Mapping[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute a direct, scope-limited, sequential GSC mutation batch."""

    config = load_scope_config()
    client = google_api.service()
    operations = prepare_operations(raw_operations, config, client)
    op_hash = operation_hash(operations)
    scope = operation_scope(operations)

    if dry_run:
        return {
            "contract_version": CRUD_CONTRACT_VERSION,
            "runtime": "PYTHON_FASTMCP_HORIZON",
            "mode": "DRY_RUN",
            "execution_status": "NOT_EXECUTED",
            "execution_attempted": False,
            "atomic": False,
            "execution_strategy": "SEQUENTIAL_STOP_ON_FIRST_ERROR",
            "operation_count": len(operations),
            "operations": public_operations(operations),
            "operation_scope": scope,
            "operation_hash": op_hash,
            "operation_hash_version": OPERATION_HASH_VERSION,
            "results": [],
            "verification": {
                "request_shapes_built": True,
                "precondition_reads_performed": True,
                "allowlists_verified": True,
                "google_api_mutation_sent": False,
                "post_mutation_reads_performed": False,
            },
        }

    results: list[dict[str, Any]] = []
    execution_failure: dict[str, Any] | None = None
    mutation_attempts = 0
    verification_reads = 0

    for index, operation in enumerate(operations):
        no_op_reason = operation["no_op_reason"]
        if no_op_reason:
            results.append(
                {
                    "operation_index": index,
                    "action": operation["action"],
                    "resource": operation["resource"],
                    "site_url": operation["site_url"],
                    "resource_name": operation["resource_name"],
                    "execution_status": "SUCCEEDED",
                    "outcome": no_op_reason,
                    "response": None,
                    "post_execution_observation": operation[
                        "precondition_state"
                    ],
                    "post_execution_verification_status": "VERIFIED",
                }
            )
            continue

        try:
            mutation_attempts += 1
            response = google_api.execute(client, operation)
            verification_reads += 1
            verification_status, observation = google_api.verify(
                client, operation
            )
            result = {
                "operation_index": index,
                "action": operation["action"],
                "resource": operation["resource"],
                "site_url": operation["site_url"],
                "resource_name": operation["resource_name"],
                "execution_status": (
                    "SUCCEEDED"
                    if verification_status == "VERIFIED"
                    else "FAILED"
                ),
                "outcome": "MUTATED",
                "response": response,
                "post_execution_observation": observation,
                "post_execution_verification_status": verification_status,
            }
            results.append(result)
            if verification_status != "VERIFIED":
                execution_failure = {
                    "operation_index": index,
                    "code": "POST_EXECUTION_VERIFICATION_FAILED",
                    "message": (
                        "The API call returned but verification did not match."
                    ),
                    "details": {},
                    "retryable": False,
                    "execution_may_have_completed": True,
                }
                break
        except Exception as exc:
            execution_failure = _failure(exc, index)
            results.append(
                {
                    "operation_index": index,
                    "action": operation["action"],
                    "resource": operation["resource"],
                    "site_url": operation["site_url"],
                    "resource_name": operation["resource_name"],
                    "execution_status": (
                        "UNKNOWN"
                        if execution_failure["execution_may_have_completed"]
                        else "FAILED"
                    ),
                    "error": execution_failure,
                }
            )
            break

    completed = sum(item["execution_status"] == "SUCCEEDED" for item in results)
    all_verified = execution_failure is None and completed == len(operations)
    return {
        "contract_version": CRUD_CONTRACT_VERSION,
        "runtime": "PYTHON_FASTMCP_HORIZON",
        "mode": "EXECUTE",
        "execution_status": "SUCCEEDED" if all_verified else "FAILED",
        "execution_attempted": mutation_attempts > 0,
        "atomic": False,
        "execution_strategy": "SEQUENTIAL_STOP_ON_FIRST_ERROR",
        "operation_count": len(operations),
        "operations_attempted": len(results),
        "operations_completed": completed,
        "operations_not_attempted": len(operations) - len(results),
        "results": results,
        "error": execution_failure,
        "operation_scope": scope,
        "operation_hash": op_hash,
        "operation_hash_version": OPERATION_HASH_VERSION,
        "verification": {
            "precondition_reads_performed": True,
            "post_mutation_reads_performed": verification_reads > 0,
            "all_requested_resources_verified": all_verified,
            "verification_failure_count": 0 if all_verified else 1,
        },
    }
