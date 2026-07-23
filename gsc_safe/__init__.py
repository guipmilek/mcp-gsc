"""Direct Google Search Console CRUD facade for Prefect Horizon."""

from .config import GscSafetyError
from .tools import (
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

__all__ = [
    "GscSafetyError",
    "gsc_add_site",
    "gsc_batch_operations",
    "gsc_crud_status",
    "gsc_delete_site",
    "gsc_delete_sitemap",
    "gsc_get_mutation_schema",
    "gsc_get_resource",
    "gsc_list_mutable_resources",
    "gsc_list_resources",
    "gsc_submit_sitemap",
]
