# Google Search Console protected CRUD

## Exact API surface

Search Console does not expose a generic update operation. This facade uses the
API's real verbs instead of inventing conventional CRUD semantics.

| Resource | Read | Create-like | Delete | Unsupported |
|---|---|---|---|---|
| `Site` | `get`, `list` | `add` | `delete` | `update`, `archive`, `restore` |
| `Sitemap` | `get`, `list` | `submit` | `delete` | `update`, `archive`, `restore` |
| `SearchAnalytics` | `query` | — | — | all writes |
| `URLInspection` | `inspect` | — | — | all writes |

Legacy direct mutation tools are intentionally not registered by
`horizon_server.py`. Managed deployments must use:

- `gsc_create_resource`
- `gsc_delete_resource`
- `gsc_batch_operations`

## Safety sequence

1. Read current resource state.
2. Validate resource, action, gate, site allowlist and sitemap prefix allowlist.
3. Build a deterministic operation payload and precondition hashes.
4. Return a local `CONNECTOR_PREFLIGHT` receipt with an HMAC confirmation.
5. Require a second call with `validate_only=false` and the exact receipt.
6. Verify token version, key ID, signature, TTL, operation hash and preconditions.
7. Register process-local replay state before the Google API call.
8. Execute sequentially and stop after the first failure.
9. Read requested resources again and report verification evidence.

The Search Console API has no native dry-run endpoint for these operations.
`validate_only=true` means connector-side preflight only.

## Required environment variables

```env
GSC_ADMIN_MUTATIONS_ENABLED=false
GSC_ALLOW_SITE_ADD=false
GSC_ALLOW_SITE_DELETE=false
GSC_ALLOW_SITEMAP_SUBMIT=false
GSC_ALLOW_SITEMAP_DELETE=false

GSC_ALLOWED_SITE_URLS=sc-domain:example.com
GSC_ALLOWED_SITEMAP_PREFIXES=https://www.example.com/

GSC_MAX_OPERATIONS_PER_REQUEST=10
GSC_CONFIRMATION_TTL_SECONDS=900
GSC_CONFIRMATION_SECRET=<at-least-32-strong-bytes>
```

For managed deployments, credentials may be supplied as:

```env
GOOGLE_APPLICATION_CREDENTIALS_JSON_BASE64=<base64-service-account-json>
```

The Horizon entrypoint materializes this value with mode `0600`, sets
`GSC_CREDENTIALS_PATH`, and defaults `GSC_SKIP_OAUTH=true`.

## Optional key rotation

During a controlled rotation only:

```env
GSC_CONFIRMATION_PREVIOUS_SECRET=<old-secret>
```

Remove it after all pre-rotation receipts have expired. Diagnostics expose only
non-secret key fingerprints.

## Operational properties

```text
operation_hash_version: 3
confirmation_token_version: 2
replay_protection: BEST_EFFORT_PROCESS_LOCAL
globally_single_use: false
atomic: false
execution_strategy: SEQUENTIAL_STOP_ON_FIRST_ERROR
validation_kind: CONNECTOR_PREFLIGHT
```

All replicas must use the same current confirmation secret. For globally
single-use receipts, replace the process-local replay set with shared durable
storage before horizontally scaling mutation traffic.
