"""Read/preflight/confirm/execute/verify coordinator for GSC mutations."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from . import google_api
from .config import (
    CONFIRMATION_TOKEN_VERSION,
    OPERATION_HASH_VERSION,
    RESOURCE_REGISTRY,
    GscSafetyError,
    SafetyConfig,
    assert_allowed_site,
    assert_allowed_sitemap,
    assert_mutation_allowed,
    canonical_json,
    canonical_site_url,
    canonical_sitemap_url,
    load_safety_config,
    sha256_json,
)
from .confirmations import (
    PROCESS_INSTANCE_ID,
    assert_not_replayed,
    issue_confirmation,
    verify_confirmation,
)


def normalize_operation(raw: Mapping[str, Any], config: SafetyConfig) -> dict[str, Any]:
    action = str(raw.get("action") or "").strip().lower()
    resource = str(raw.get("resource") or "").strip()
    if resource not in RESOURCE_REGISTRY:
        raise GscSafetyError("UNSUPPORTED_RESOURCE", f"Unsupported resource: {resource!r}.")
    if action not in RESOURCE_REGISTRY[resource]["actions"]:
        raise GscSafetyError(
            "UNSUPPORTED_ACTION", f"Action {action!r} is not supported for {resource}."
        )
    if action in {"get", "list"}:
        raise GscSafetyError("INVALID_ARGUMENT", "Read actions are not valid batch mutations.")

    site_url = canonical_site_url(str(raw.get("site_url") or raw.get("resource_name") or ""))
    assert_allowed_site(config, site_url)
    assert_mutation_allowed(config, action, resource)
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
        raise GscSafetyError("INVALID_ARGUMENT", "Site operations do not accept data fields.")
    return operation


def prepare_operations(
    raw_operations: Iterable[Mapping[str, Any]], config: SafetyConfig
) -> list[dict[str, Any]]:
    raw_list = list(raw_operations)
    if not raw_list:
        raise GscSafetyError("INVALID_ARGUMENT", "At least one operation is required.")
    if len(raw_list) > config.max_operations_per_request:
        raise GscSafetyError(
            "TOO_MANY_OPERATIONS",
            "The request exceeds GSC_MAX_OPERATIONS_PER_REQUEST.",
            {"requested": len(raw_list), "maximum": config.max_operations_per_request},
        )

    operations = [normalize_operation(raw, config) for raw in raw_list]
    client = google_api.service()
    for operation in operations:
        state = google_api.precondition_state(client, operation)
        operation["precondition_state"] = state
        operation["precondition_hash"] = sha256_json(state)
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


def preconditions_hash(operations: list[dict[str, Any]]) -> str:
    return sha256_json([item["precondition_hash"] for item in operations])


def public_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "action": item["action"],
            "resource": item["resource"],
            "site_url": item["site_url"],
            "resource_name": item["resource_name"],
            "data": item["data"],
            "precondition_hash": item["precondition_hash"],
        }
        for item in operations
    ]


def scope(operations: list[dict[str, Any]]) -> dict[str, Any]:
    actions: dict[str, int] = {}
    resources: dict[str, int] = {}
    for item in operations:
        actions[item["action"]] = actions.get(item["action"], 0) + 1
        resources[item["resource"]] = resources.get(item["resource"], 0) + 1
    return {
        "actions": actions,
        "resources": resources,
        "requested_resource_names": [item["resource_name"] for item in operations],
        "contains_delete": any(item["action"] == "delete" for item in operations),
        "atomic": False,
        "execution_strategy": "SEQUENTIAL_STOP_ON_FIRST_ERROR",
    }


def refresh_preconditions(operations: list[dict[str, Any]]) -> None:
    client = google_api.service()
    for operation in operations:
        current = google_api.precondition_state(client, operation)
        current_hash = sha256_json(current)
        if current_hash != operation["precondition_hash"]:
            raise GscSafetyError(
                "PRECONDITION_CHANGED",
                "The resource changed after preflight; generate a new preflight.",
                {
                    "resource": operation["resource"],
                    "resource_name": operation["resource_name"],
                    "expected_precondition_hash": operation["precondition_hash"],
                    "current_precondition_hash": current_hash,
                },
            )


def coordinate(
    raw_operations: Iterable[Mapping[str, Any]],
    *,
    validate_only: bool,
    confirmation: str | None,
) -> dict[str, Any]:
    if not validate_only and confirmation:
        assert_not_replayed(confirmation)

    config = load_safety_config()
    operations = prepare_operations(raw_operations, config)
    op_hash = operation_hash(operations)
    pph = preconditions_hash(operations)
    operation_scope = scope(operations)

    if validate_only:
        receipt, expires, confirmation_key_id = issue_confirmation(
            config, op_hash, pph, [item["site_url"] for item in operations]
        )
        return {
            "runtime": "PYTHON_FASTMCP_HORIZON",
            "mode": "VALIDATE_ONLY",
            "validation_kind": "CONNECTOR_PREFLIGHT",
            "admin_api_validate_only_supported": False,
            "validation_status": "PASSED",
            "validated": True,
            "validated_in_current_call": True,
            "execution_attempted": False,
            "executed": False,
            "execution_status": "NOT_EXECUTED",
            "operation_count": len(operations),
            "normalized_operations": public_operations(operations),
            "operation_scope": operation_scope,
            "confirmation_expires_at": expires.isoformat(),
            "verification": {
                "request_shapes_built": True,
                "precondition_reads_performed": True,
                "allowlists_verified": True,
                "google_api_mutation_sent": False,
                "post_mutation_read_performed": False,
            },
            "operation_hash": op_hash,
            "operation_hash_version": OPERATION_HASH_VERSION,
            "confirmation_token_version": CONFIRMATION_TOKEN_VERSION,
            "confirmation_key_id": confirmation_key_id,
            "confirmation_issued_by_process_instance_id": PROCESS_INSTANCE_ID,
            "required_confirmation": receipt,
            "validation_receipt": {
                "confirmation_key_id": confirmation_key_id,
                "issued_by_process_instance_id": PROCESS_INSTANCE_ID,
                "cross_instance_valid": None,
                "cross_instance_requirement": "MATCHING_CONFIRMATION_KEY_ID",
                "replay_protection": "BEST_EFFORT_PROCESS_LOCAL",
                "globally_single_use": False,
                "expires_at": expires.isoformat(),
            },
        }

    verified = verify_confirmation(config, confirmation or "", op_hash, pph)
    refresh_preconditions(operations)
    client = google_api.service()
    results: list[dict[str, Any]] = []
    execution_failure: dict[str, Any] | None = None

    for index, operation in enumerate(operations):
        try:
            response = google_api.execute(client, operation)
            verification_status, observation = google_api.verify(client, operation)
            result = {
                "action": operation["action"],
                "resource": operation["resource"],
                "site_url": operation["site_url"],
                "resource_name": operation["resource_name"],
                "response": response,
                "post_execution_observation": observation,
                "post_execution_verification_status": verification_status,
                "operation_index": index,
                "execution_status": "SUCCEEDED" if verification_status == "VERIFIED" else "FAILED",
            }
            results.append(result)
            if verification_status != "VERIFIED":
                execution_failure = {
                    "operation_index": index,
                    "code": "POST_EXECUTION_VERIFICATION_FAILED",
                    "message": "The API call returned but verification did not match.",
                }
                break
        except Exception as exc:
            execution_failure = {
                "operation_index": index,
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
            }
            results.append(
                {
                    "action": operation["action"],
                    "resource": operation["resource"],
                    "site_url": operation["site_url"],
                    "resource_name": operation["resource_name"],
                    "operation_index": index,
                    "execution_status": "FAILED",
                    "error": execution_failure,
                }
            )
            break

    completed = sum(1 for item in results if item["execution_status"] == "SUCCEEDED")
    attempted = len(results)
    all_verified = execution_failure is None and completed == len(operations)
    return {
        "runtime": "PYTHON_FASTMCP_HORIZON",
        "mode": "EXECUTE",
        "validation_status": "PRIOR_VALIDATION_VERIFIED",
        "execution_attempted": attempted > 0,
        "executed": completed > 0,
        "execution_status": "SUCCEEDED" if all_verified else "FAILED",
        "atomic": False,
        "execution_strategy": "SEQUENTIAL_STOP_ON_FIRST_ERROR",
        "operation_count": len(operations),
        "operations_attempted": attempted,
        "operations_completed": completed,
        "operations_not_attempted": len(operations) - attempted,
        "results": results,
        "execution_failure": execution_failure,
        "operation_scope": operation_scope,
        "verification": {
            "post_execution_reads_performed": attempted > 0,
            "all_requested_resources_verified": all_verified,
            "verification_failure_count": 0 if all_verified else 1,
            "claims_limited_to_requested_resources": True,
        },
        "confirmation_verified": True,
        "confirmation_registered_before_api_call": True,
        "confirmation_token_fingerprint": verified.token_fingerprint,
        "confirmation_token_version": CONFIRMATION_TOKEN_VERSION,
        "confirmation_key_id": verified.key_id,
        "confirmation_key_source": verified.key_source,
        "confirmation_issued_by_process_instance_id": verified.issued_by_process_instance_id,
        "confirmation_verified_by_process_instance_id": PROCESS_INSTANCE_ID,
        "operation_hash": op_hash,
        "operation_hash_version": OPERATION_HASH_VERSION,
    }
