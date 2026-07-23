# Direct Google Search Console CRUD

The Prefect Horizon server exposes `direct-crud-v1`. Mutations run in one tool
call and never require a prepare step, approval code, confirmation secret, or
action gate.

## Public tools

Read and discovery:

- `gsc_crud_status`
- `gsc_list_mutable_resources`
- `gsc_get_mutation_schema`
- `gsc_get_resource`
- `gsc_list_resources`

Direct writes:

- `gsc_add_site`
- `gsc_delete_site`
- `gsc_submit_sitemap`
- `gsc_delete_sitemap`
- `gsc_batch_operations`

Every write accepts optional `dry_run=false`. A dry run performs canonical
input checks, allowlist checks, and API precondition reads without sending a
mutation.

Search Console exposes add/get/list/delete for Sites and
submit/get/list/delete for Sitemaps. It does not expose update, archive, or
restore operations.

Delete tools are idempotent. Deleting an already absent Site or Sitemap returns
success with `ALREADY_ABSENT`.

## Required Horizon configuration

```text
GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64=<base64-service-account-json>
GSC_ALLOWED_SITE_URLS=sc-domain:example.com,https://staging.example.com/
GSC_ALLOWED_SITEMAP_PREFIXES=https://www.example.com/,https://staging.example.com/
GSC_MAX_OPERATIONS_PER_REQUEST=10
```

The old `GSC_ADMIN_MUTATIONS_ENABLED`, `GSC_ALLOW_*`, and
`GSC_CONFIRMATION_*` variables are ignored and should be removed.

## ChatGPT workspace actions

After deploying a changed tool catalog, refresh the custom app in ChatGPT
Workspace Settings, enable all five direct write actions, and configure the
desired per-app approval mode. Search Console delete actions remain truthfully
annotated as destructive and idempotent; client workspace policy is configured
in ChatGPT rather than bypassed inside the MCP server.

## Execution behavior

Batches are non-atomic and stop on the first failed or uncertain operation.
Each successful mutation is followed by a read that verifies the requested
resource is present or absent. Transport errors report whether execution may
have completed so callers do not retry uncertain writes blindly.
