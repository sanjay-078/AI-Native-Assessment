# AI Native Developer Assessment

**Focused effort:** approximately five hours across 29 October 2026, estimated from the working sessions; exact stopwatch time was not kept. The initial core was recorded as about one hour, followed by evaluation, hardening, and this completion pass.

**Completion check (29 October 2026):** the required behavior passed 20 application tests, 14/14 fixture smoke checks, 16/16 fresh real-model development cases, 12/12 fresh real-model public audit cases, and five targeted real-model policy cases. The earlier failed insurance-fee runs and original 8/12 audit remain in the evidence as historical failures. Known limits are listed in `EVAL.md`; this is a synthetic assessment, not a production capacity or semantic-accuracy guarantee. Optional depth beyond the recorded demonstrations was not pursued.

## Run

From a clean checkout, copy `.env.example` to an ignored `.env`, then run:

```sh
docker compose up -d --build
make seed
make test
```

The API is at `http://localhost:8090`; the desktop review UI is at `http://localhost:8090/ui/`. Use a reader or reviewer token from the supplied `kit/data/auth.json`. The UI keeps the token in page memory and uses the same authenticated API routes. It is designed for desktop use only.

`make seed` imports `${ASSESSMENT_DATA_DIR:-./kit/data}` into PostgreSQL safely on repeat. The API and worker mount that folder read-only at `/assessment-data`. `make test` runs the application tests inside containers. The default `.env.example` uses the deterministic kit model, which is for systems checks, not an AI demonstration. With that fixture configuration, run `python3 kit/check_core.py`; development cases can be run with `python3 kit/evaluate.py --out results/dev.jsonl`.

For the actual local model, start Ollama on the host with `qwen2.5:3b` available and set these values in the ignored `.env`:

```text
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:3b
```

Recreate the worker with `docker compose up -d --force-recreate worker`. The actual-model development command is `python3 kit/evaluate.py --out results/dev.jsonl`; the declared public audit command is `python3 kit/evaluate.py --cases kit/data/audit.jsonl --out results/audit.jsonl`. The original frozen audit and later labelled reruns are retained under `results/`; do not overwrite them. Never commit `.env` or actual keys.

## Architecture and guarantees

FastAPI handles authentication and the four application routes. PostgreSQL stores tenant-scoped requests, unique request keys, policy documents, run checkpoints, deferred retry times, traces, and approval/action state. A worker claims due rows with `SKIP LOCKED` and a 20-second lease. The default Compose command starts one worker; two workers were demonstrated in the recorded load checks. Acknowledged queued or interrupted work is reclaimable. A rate-limited model or payment-tool call records its attempt and returns the run to the durable queue until `Retry-After` expires, so no worker sleeps past its lease. Durable counters bound a run to six model calls, three read-tool calls, one malformed-output repair, ten-second call timeouts, and a 60-second active deadline. Admission is capped at 16 outstanding runs per tenant by default, returning 429 and `Retry-After` when full. A crash after a model request is sent but before its response is saved can repeat that call and incur extra model cost; the durable call reservation still keeps the run bounded.

Retrieval selects only the authenticated tenant's active policy rows. For a payment request, the model selects `get_payment_status`; the backend validates its arguments and adds the authenticated tenant before the read. The server validates the result against the tool outcome and active policy and emits exact source excerpts. For policy questions, a candidate document must cover each material question term before the answer can cite it; a related fee document alone cannot establish an insurance policy. Missing or unsupported cases request human review; paid payments have no executable proposal.

A held payment's proposed follow-up is separate from the immutable run result. Its SHA-256 hash covers canonical UTF-8 JSON with keys `type`, `tenant_id`, `payment_id`, and `reason`, sorted keys and compact separators, with non-ASCII kept literal. Only a same-tenant reviewer can approve the stored hash. The worker rechecks that the payment is held, uses the action ID as a stable external idempotency key, and reconciles a lost response against the receipt. Approval and receipt survive restarts.

The trace shows retrieval IDs, model attempts, validated tool calls, decisions, and receipts without auth headers or keys. `EVAL.md` records actual-model quality, systems evidence, and limitations; `RECOVERY.md` gives the failure replay. This uses synthetic assessment data and is not a production payment system.

## Local machine used

Apple M1, 8 CPU cores, 8 GiB RAM, about 32 GiB free disk at preflight. Colima provided Docker; the application image used Python 3.12, PostgreSQL 16, and local Ollama `qwen2.5:3b`. Host checks used Python 3.13.
