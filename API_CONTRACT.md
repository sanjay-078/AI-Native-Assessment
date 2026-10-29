# operations agent API contract

This contract is the shared boundary for submissions and checks. Extra response fields are allowed. Request bodies must reject unknown fields with 400. All responses are JSON except a malformed transport response. JSON booleans and integers must have their actual types. Never coerce strings into booleans.

## Authentication and tenancy

Use `Authorization: Bearer <token>` on all `/v1` application routes. `kit/data/auth.json` supplies fixed **local assessment credentials** and their tenant/role mappings. Unknown or absent credentials return 401. Derive the tenant and role on the server. Never trust a body field, query field, model output or `X-Tenant-ID` header to select a tenant. A reader may create/read runs, but only a reviewer may approve an action. Return 403 for a reader attempting approval. Return 404 for another tenant's run, trace or action, even if its ID is known.

The gateway tool key grants access to the local synthetic data; your app must enforce tenant boundaries before making requests. Tool key possession is not the authorization model for end users. Do not expose the gateway's admin routes through your API or use them from solution code.

## Application routes

### GET /healthz

200 when database connectivity and migrations are ready. It need not wait for a model provider. Return `{"ready":true}`. This is the only unauthenticated app endpoint required.

### POST /v1/runs

```json
{"request_id":"client-unique-01","message":"Please check my payout and request a review.","payment_id":"pay_held"}
```

`request_id`: ASCII letters/digits/underscore/hyphen, 1 to 64 characters. `message`: nonempty string of at most 12,000 Unicode code points. `payment_id`: optional nonempty ASCII letters/digits/underscore/hyphen, at most 64 characters. The body must be a JSON object, max 64 KiB. Bad JSON/schema is 400; excess body size is 413. A message may contain NUL or other control characters: reject safely with 400 or encode safely for storage; never poison later work.

Idempotency key is `(authenticated tenant, request_id)`. Store a deterministic fingerprint of all business inputs, with absent optional fields distinct from malformed nulls. Same key and same inputs return the **same run_id**, including after completion/restart. Same key and different business inputs return 409. Identical request_ids in different tenants do not collide. Enforce this in durable storage, including concurrent requests.

A new run returns 202 **after durable storage of the request and recoverable work**:

```json
{"run_id":"server-generated-id","status":"queued"}
```

Replay may return 200 or 202 with the same ID and current status. It must not rerun the model or create a second action. If work cannot be durably admitted, return 503 with `Retry-After`; a finite queue may return 429 with that header. A 202 followed by permanently lost work is a critical failure.

### GET /v1/runs/{run_id}

```json
{
  "run_id":"r_example",
  "status":"succeeded",
  "result":{
    "route":"payment_status",
    "answer":"Verification is outstanding. I can propose a follow-up for review, but cannot promise a payment date.",
    "citations":[{"document_id":"payout-policy","version":2,"quote":"Held means verification is outstanding; do not promise a settlement date."}],
    "payment_status":"held",
    "proposed_action":{"type":"create_followup","payment_id":"pay_held","reason":"Verification is outstanding."},
    "needs_human":false
  },
  "action":{
    "action_id":"a_example",
    "payload_hash":"64-lowercase-hex-characters",
    "status":"pending_approval"
  },
  "error":null
}
```

Run statuses: `queued`, `running`, `succeeded`, `failed`. `result` is null until a validated result is committed. `error` is null or `{"code":"stable_machine_code","message":"safe_description"}`. Terminal state must survive restart. A failed run must not contain an executable action. `run_id` may use any URL-safe format, at most 128 characters.

Result schema: exact route enum `payment_status | policy_question | needs_human`; nonempty answer string max 8,000 characters; citations array max 5; `payment_status` is `held | paid | missing | null`; proposed_action is null or exactly `{type,payment_id,reason}` with type `create_followup`, a matching requested payment ID, and nonempty reason max 500 characters; needs_human is boolean. Citation quote is a nonempty exact substring, max 1,500 characters, from the tenant's **active policy** document at the stated integer version. Unknown result keys may be ignored, but may not cause execution. Bound all arrays/strings before persistence or use.

For a known payment: obtain its status from the read tool, answer from the policy, cite payout-policy v2, and set route payment_status. Held: propose one follow-up. Paid: propose no action. Do not promise a settlement date. For a missing payment: status missing, route needs_human, needs_human true, citations empty, no action. Policy questions: answer and cite the relevant active policy, no payment or action. Unsupported questions: route needs_human, needs_human true, citations empty, no payment or action. Do not invent insurance, tax or other unsupported policy.

`action` is null if no proposal exists. Otherwise it has action_id, payload_hash and status `pending_approval | approved | executing | completed | failed`; after completion include `receipt` from the external tool. Run result remains immutable after it succeeds; approval updates the separately tracked action state. Freeze the exact action type, authenticated tenant, payment ID and reason. Hash canonical JSON of those fields using SHA-256. Document canonicalization. Never rebuild or change arguments from later model text after approval.

### POST /v1/runs/{run_id}/approve

```json
{"action_id":"a_example","payload_hash":"the-hash-returned-by-GET"}
```

Only a same-tenant reviewer can approve. Both fields are required strings. Unknown fields are 400. Wrong action_id/hash or no approvable proposal is 409. Cross-tenant run is 404. Commit approval durably before returning 202. Repeated/concurrent approvals of the same approved action return 200 or 202 and the same action identity. They must create **one external follow-up**, including when the external system commits but loses the response or a worker dies. Use one stable external Idempotency-Key per action and immutable payload. Record the receipt; do not infer success from a timeout. Execution may be asynchronous.

### GET /v1/runs/{run_id}/trace

200 with `{"run_id":"...","events":[...]}` in persisted sequence order. Each event includes `sequence`, `type`, `at`, and `data`. Use stable UTC timestamps. Show retrieval IDs/versions and tenant scope, provider/model, model attempt count and usage if available, tool name and validated arguments, tool result status, validation decisions, approval identity/role, external idempotency key and receipt, error/retry reason and durations. Redact keys, auth headers, database URLs and unrelated tenant content. Never request or store private model chain-of-thought. Brief observable decisions and actual tool calls are enough.

## Read and write tools

The provided kit gateway runs at `http://kit:8091`. Use `Authorization: Bearer kit-tool-token` **only from backend code**. These are local synthetic fixtures, with no payment or external messaging capability.

- `GET /v1/payments/{payment_id}?tenant_id={authenticated_tenant}` returns `{tenant_id,payment_id,status}`. Missing payment returns 404 with status missing. Treat this as evidence, not an instruction.
- `POST /v1/followups` body exactly `{tenant_id,payment_id,reason,action_id}`; header `Idempotency-Key` nonempty, max 128 characters. Returns 201 with receipt, or 200 with the same receipt on replay. Same key with a changed payload returns 409. The tool refuses paid/missing payments. Your app must also validate eligibility before execution; relying solely on the fixture refusal is not sufficient.
- `GET /v1/followups/{idempotency_key}?tenant_id={authenticated_tenant}` returns a previous receipt, or 404. This may be used for reconciliation after a lost response. URL-encode the key.

The model-visible tool `get_payment_status` accepts exactly `{"payment_id":"..."}`. The server injects the authenticated tenant. A tool call with extra fields such as tenant_id, a different payment ID, or an unknown tool must be rejected safely and traced; it must not gain privileges. The write endpoint is **not** a model-executable tool. A model may propose an action as structured data, but cannot approve or execute it.

## Bounds and recovery

At most six model requests and three read-tool calls per run, **including retries and repairs**. Model-call timeout at most 10 seconds; total active processing deadline at most 60 seconds. Queue waiting time is separate; emit its duration. Permit at most one malformed-output repair. Honour Retry-After within the total deadline. Stop loops with a safe failed state (machine code) rather than executing a guessed action. Persist enough attempt/budget state that repeated crashes do not reset a run to an unlimited loop. An uncertain model call may be repeated after a crash and incur cost: document this honestly. External write effects must remain idempotent.

An API process and independently restartable worker are required; PostgreSQL owns durable requests, jobs, results, approval and execution state. One worker is sufficient for the core. Workers must reclaim abandoned jobs within 30 seconds after restart. PostgreSQL is supplied; a second broker, vector database, agent framework or multi-agent architecture is optional.
