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

## Horizon deployment: at most two keys

```text
MCP_CREDENTIALS=<base64-encoded credential envelope>
# Optional restriction:
MCP_CONFIG={"sites":["sc-domain:example.com"],"sitemaps":["https://www.example.com/"],"max_operations":10}
```

The decoded credential envelope is:

```json
{"google_credentials":{"type":"service_account","project_id":"..."}}
```

`google_credentials` contains the complete Google credential JSON. No
additional GSC credential, allowlist, limit, mutation, or confirmation
variables are used by the Horizon entrypoint.

`MCP_CONFIG` is optional. When it is absent, or its allowlist arrays are
absent or empty, all Search Console resources accessible to the credential are
allowed and the default batch limit is 10. Supply it only to narrow scope or
change the batch limit. The old
`GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64`, `GSC_ALLOWED_*`, `GSC_MAX_*`,
`GSC_ADMIN_MUTATIONS_ENABLED`, `GSC_ALLOW_*`, and `GSC_CONFIRMATION_*`
deployment variables should be removed.

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
