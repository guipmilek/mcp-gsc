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
`horizon_server.py`. The generic Python facade also remains internal and is not
published in the Horizon tool catalog.

Managed deployments expose dedicated, fixed-schema tools:

| Action | Read-only preflight | Confirmed execution |
|---|---|---|
| `Site.add` | `gsc_prepare_site_add` | `gsc_execute_site_add` |
| `Site.delete` | `gsc_prepare_site_delete` | `gsc_execute_site_delete` |
| `Sitemap.submit` | `gsc_prepare_sitemap_submit` | `gsc_execute_sitemap_submit` |
| `Sitemap.delete` | `gsc_prepare_sitemap_delete` | `gsc_execute_sitemap_delete` |

The Horizon catalog does **not** expose:

- `gsc_create_resource`
- `gsc_delete_resource`
- `gsc_batch_operations`
- `add_site`
- `delete_site`
- `manage_sitemaps`

This separation prevents a preflight from being classified as a generic write
tool and prevents callers from selecting `resource`, `action`, `data`, or
`validate_only` dynamically.

## Short approval codes

Prepare tools return `required_approval_code`. Execute tools accept only the
corresponding `approval_code` plus the fixed resource arguments.

The code format is deliberately compact and non-JWT-like:

```text
GSC3-<key-id>-<expiry>-<nonce>-<truncated-hmac>
```

The code contains no credential or HMAC secret. Its HMAC is bound to:

- operation hash;
- full precondition hash;
- allowlisted site URLs;
- expiration;
- nonce;
- signing-key fingerprint.

The execute tool recalculates the operation and current preconditions before
verifying the code. Codes remain valid across replicas that share the same
current signing key. Replay registration remains process-local and therefore is
not globally single-use across replicas.

## MCP annotations

All Horizon tools declare the complete standard annotation set.

Preflight tools:

```text
readOnlyHint: true
destructiveHint: false
idempotentHint: true
openWorldHint: false
```

Additive execution tools (`Site.add`, `Sitemap.submit`):

```text
readOnlyHint: false
destructiveHint: false
idempotentHint: true
openWorldHint: false
```

Delete execution tools:

```text
readOnlyHint: false
destructiveHint: true
idempotentHint: true
openWorldHint: false
```

`openWorldHint=false` reflects that mutable operations are constrained to the
configured exact site allowlist and sitemap prefix allowlist. Annotations are
hints only; the connector still enforces all gates, allowlists, approval codes,
preconditions, replay checks, and post-execution verification.

## Safety sequence

1. Call the matching `gsc_prepare_*` tool.
2. Read current resource state.
3. Validate action gate, exact site allowlist and sitemap prefix allowlist.
4. Build a deterministic operation payload and precondition hashes.
5. Return a local `CONNECTOR_PREFLIGHT` result with a short HMAC approval code.
6. Call the matching `gsc_execute_*` tool with the same fixed arguments and the
   exact `approval_code`.
7. Verify code version, key ID, signature, TTL, operation hash and preconditions.
8. Register process-local replay state before the Google API call.
9. Execute the single requested operation.
10. Read the requested resource again and report verification evidence.

The Search Console API has no native dry-run endpoint for these operations.
The `gsc_prepare_*` tools perform connector-side preflight only and cannot
execute a mutation. The `gsc_execute_*` tools do not expose a `validate_only`
switch and reject a missing approval code before making an API read.

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

Remove it after all pre-rotation approval codes have expired. Diagnostics expose
only non-secret key fingerprints.

## Operational properties

```text
operation_hash_version: 3
confirmation_token_version: 3
confirmation_format: SHORT_HMAC_APPROVAL_CODE
cross_instance_valid: true with matching key ID
replay_protection: BEST_EFFORT_PROCESS_LOCAL
globally_single_use: false
atomic: false
execution_strategy: SEQUENTIAL_STOP_ON_FIRST_ERROR
validation_kind: CONNECTOR_PREFLIGHT
```

All replicas must use the same current confirmation secret. For globally
single-use approval codes, replace the process-local replay set with shared
durable storage before horizontally scaling mutation traffic.
