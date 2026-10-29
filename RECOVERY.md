# Failure and recovery replay

All commands were run from the project root with PostgreSQL and the kit gateway up. The kit admin endpoint was used only by this host-side drill, never by application code.

## Acknowledged queued work

1. `docker compose stop worker`
2. POST `/v1/runs` as the Atlas reviewer with a new request ID and `What is the standard service fee?`. The API returned 202 and the run stayed `queued`.
3. `docker compose start worker`
4. GET the run until terminal: it became `succeeded` with retrieval, model and validation events. The run ID and statuses are in `results/restart-run.json`.

## Kill during an in-flight read

1. In fixture mode, the host test set `read_fault=timeout_once` through `/__admin/config`.
2. POST an Atlas `pay_held` request; wait until its trace shows `tool_selected`, before `tool_result`.
3. `docker compose kill worker`, confirm the acknowledged run remains `running`, then `docker compose start worker`.
4. The worker reclaimed the expired 20-second lease and completed the run 19.89 seconds after restart. Its trace has two claims; durable counters are 3 model calls and 2 read reservations, including the interrupted attempt. See `results/running-recovery.json`.

The public smoke test separately injected `write_fault=commit_then_disconnect` during concurrent approvals. The worker reconciled the external receipt using the stable action key; the gateway reported one committed follow-up. See `results/check-core.txt`.

## Long rate-limit retry with two workers

`app/app/tests/test_retry_deferral.py` sends a synthetic model 429 with `Retry-After: 25`, longer than the worker's 20-second lease. The production model-call function reserves one durable attempt and raises a deferral. The worker stores a due time, changes the run to `queued`, and releases its lease. Two separate worker claim calls cannot take that row while its due time is in the future, even when the former lease is forced to appear expired. The test checks the one-attempt counter and deletes its synthetic row. A second test checks the payment-tool 429 path; a third persists a selected payment read, defers it, and resumes it without another model tool selection. `make test` passed all 20 tests in the final repair run. The delay is simulated, so this proves the database and worker state transition without waiting 25 wall-clock seconds or claiming that Ollama emitted that header.
