"""Protected Google Search Console mutation facade."""

from .crud import (
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

__all__ = [
    "GscSafetyError",
    "gsc_batch_operations",
    "gsc_confirmation_diagnostics",
    "gsc_create_resource",
    "gsc_delete_resource",
    "gsc_get_mutation_schema",
    "gsc_get_resource",
    "gsc_list_mutable_resources",
    "gsc_list_resources",
    "gsc_safety_status",
]
