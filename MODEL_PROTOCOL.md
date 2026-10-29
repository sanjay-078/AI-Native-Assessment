# Model protocol and fixture mode

Your solution must support an actual model through `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. An OpenAI-compatible Chat Completions adapter is the common interchange format for this assessment. You may additionally implement another provider adapter. No particular model, SDK or orchestration framework earns points.

POST `{LLM_BASE_URL}/chat/completions` with model, messages and a function tool named get_payment_status (required payment_id string; no extra properties). Parse assistant tool_calls, validate names/arguments on the server, execute the allowed read tool and append standard assistant/tool messages. Then ask for a final JSON result matching API_CONTRACT.md. The model must actually choose the payment lookup in payment cases and use the returned evidence. A fixed classifier or keyword router with a decorative unused model call is insufficient.

## Working without provider credentials

The kit has a deterministic fixture at `http://kit:8091/v1/chat/completions`, key `kit-model-token`, model `fixture`. This tests orchestration, failure handling and HTTP/schema boundaries; **it is not an AI model and earns no live-AI credit**. For fixture compatibility, add one message whose complete content is `ASSESSMENT_CONTEXT=` followed by a JSON object:

```json
{"run_id":"your-id","tenant_id":"atlas","request":{"message":"Please check this payout.","payment_id":"pay_held"},"evidence":[{"tenant_id":"atlas","document_id":"payout-policy","version":2,"status":"active","authority":"policy","text":"Your actual retrieved policy text"}]}
```

Keep any task instructions in separate messages; the fixture parses the JSON to the end of this message. Supply only evidence you actually retrieved and authorized. It does not retrieve for you. The fixture uses simple English heuristics and will not pass all real-language cases. This is intentional. Do not copy its heuristics into a claimed AI implementation.

Real-provider mode may use the same structured context or your own prompt format, but must use the same application state machine, validation and action boundaries. Demonstrate a run using an actual hosted or locally served model, including model name, settings, raw observable outputs and measured evaluation. Hosted access must be arranged by the hiring team before the clock starts, or use a local model you already have. Nobody is expected to buy credits or GPU access for the assessment. If access fails, tell the recruiter immediately; the live-AI portion is unverified until it can be rerun, not silently replaced with fixtures.

## Fault controls for tests

Host calls use `Authorization: Bearer kit-admin-token`. These controls are for test scripts only, never application code. `POST /__admin/config` accepts one or more of:

```json
{"model_fault":"invalid_json_once","read_fault":"rate_limit_once","write_fault":"commit_then_disconnect"}
```

Model faults: invalid_json_once, rate_limit_once, unknown_tool, foreign_tenant, loop. `loop` persists until `{"model_fault":null}`. Other model faults apply to the next model request only. Read faults: timeout_once (12-second delay), rate_limit_once. Write fault: commit_then_disconnect; the receipt is durably committed, then the socket closes before a response. Retry/reconcile using the same key. Setting a fault to null clears it. Run these tests serially on an idle system so another run cannot consume the intended fault.

`GET /__admin/audit` returns fixture call events and committed receipts. Observe model/tool boundaries and count effects; do not modify the gateway to improve results. The audit intentionally contains no provider credentials. It persists in the kit-state volume.
