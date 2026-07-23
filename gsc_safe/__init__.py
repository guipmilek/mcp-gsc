"""Protected Google Search Console mutation facade."""

from .config import GscSafetyError
from .tools import (
    gsc_batch_operations,
    gsc_confirmation_diagnostics,
    gsc_create_resource,
    gsc_delete_resource,
    gsc_execute_site_add,
    gsc_execute_site_delete,
    gsc_execute_sitemap_delete,
    gsc_execute_sitemap_submit,
    gsc_get_mutation_schema,
    gsc_get_resource,
    gsc_list_mutable_resources,
    gsc_list_resources,
    gsc_prepare_site_add,
    gsc_prepare_site_delete,
    gsc_prepare_sitemap_delete,
    gsc_prepare_sitemap_submit,
    gsc_safety_status,
)

__all__ = [
    "GscSafetyError",
    "gsc_batch_operations",
    "gsc_confirmation_diagnostics",
    "gsc_create_resource",
    "gsc_delete_resource",
    "gsc_execute_site_add",
    "gsc_execute_site_delete",
    "gsc_execute_sitemap_delete",
    "gsc_execute_sitemap_submit",
    "gsc_get_mutation_schema",
    "gsc_get_resource",
    "gsc_list_mutable_resources",
    "gsc_list_resources",
    "gsc_prepare_site_add",
    "gsc_prepare_site_delete",
    "gsc_prepare_sitemap_delete",
    "gsc_prepare_sitemap_submit",
    "gsc_safety_status",
]
