import concurrent.futures
import uuid

import httpx
import pytest

from app import logic, worker
from app.db import tx


def test_long_retry_after_is_durable_and_not_claimed_by_second_worker(monkeypatch):
    run_id = "r_retry_test_" + uuid.uuid4().hex
    run = {"run_id": run_id, "tenant_id": "atlas", "payment_id": None}
    with tx() as conn:
        conn.execute(
            """INSERT INTO runs(run_id,tenant_id,request_id,fingerprint,message,
                                status,started_at,lease_until)
               VALUES(%s,'atlas',%s,'synthetic','What is the service fee?',
                      'running',now(),now()+interval '20 seconds')""",
            (run_id, run_id),
        )

    class RateLimitedClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            return httpx.Response(429, headers={"Retry-After": "25"},
                                  request=httpx.Request("POST", url))

    try:
        monkeypatch.setenv("LLM_MODEL", "qwen2.5:3b")
        monkeypatch.setenv("LLM_BASE_URL", "http://fake-model/v1")
        monkeypatch.setenv("LLM_API_KEY", "synthetic")
        monkeypatch.setattr(logic.httpx, "Client", RateLimitedClient)

        with pytest.raises(logic.RunDeferred) as deferred:
            logic.model_response(run, [{"role": "user", "content": "Test"}])
        assert deferred.value.seconds == 25
        worker.defer_run(run, deferred.value.seconds)

        with tx() as conn:
            row = conn.execute(
                """SELECT status,model_calls,lease_until,
                          EXTRACT(EPOCH FROM next_attempt_at-now()) AS wait_seconds
                   FROM runs WHERE run_id=%s""", (run_id,),
            ).fetchone()
            assert row["status"] == "queued" and row["model_calls"] == 1
            assert row["lease_until"] is None
            assert 20 < row["wait_seconds"] <= 25
            # Even an expired former lease cannot override the deferred time.
            conn.execute("UPDATE runs SET lease_until=now()-interval '1 second' WHERE run_id=%s",
                         (run_id,))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: worker.acquire_run(), range(2)))
        assert all(claim is None or claim["run_id"] != run_id for claim in claims)
        with tx() as conn:
            row = conn.execute("SELECT status,model_calls FROM runs WHERE run_id=%s",
                               (run_id,)).fetchone()
            assert row == {"status": "queued", "model_calls": 1}
    finally:
        with tx() as conn:
            conn.execute("DELETE FROM events WHERE run_id=%s", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id=%s", (run_id,))


def test_read_tool_long_retry_after_uses_same_deferral(monkeypatch):
    class RateLimitedClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return httpx.Response(429, headers={"Retry-After": "25"},
                                  request=httpx.Request("GET", url))

    monkeypatch.setenv("TOOL_BASE_URL", "http://fake-tool")
    monkeypatch.setenv("TOOL_API_KEY", "synthetic")
    monkeypatch.setattr(logic.httpx, "Client", RateLimitedClient)
    monkeypatch.setattr(logic, "checkpoint", lambda *args: 1)
    monkeypatch.setattr(logic, "remaining", lambda *args: 60)
    monkeypatch.setattr(logic, "trace_event", lambda *args: None)
    with pytest.raises(logic.RunDeferred) as deferred:
        logic.read_payment({"run_id": "synthetic", "tenant_id": "atlas"}, "pay_held")
    assert deferred.value.seconds == 25


def test_deferred_read_resumes_without_reselecting_tool(monkeypatch):
    run_id = "r_read_retry_test_" + uuid.uuid4().hex
    with tx() as conn:
        run = conn.execute(
            """INSERT INTO runs(run_id,tenant_id,request_id,fingerprint,message,
                                payment_id,status,started_at,lease_until)
               VALUES(%s,'atlas',%s,'synthetic','Check this payout','pay_paid',
                      'running',now(),now()+interval '20 seconds') RETURNING *""",
            (run_id, run_id),
        ).fetchone()

    call_count = 0
    read_count = 0

    def fake_model(*args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {
                "name": "get_payment_status", "arguments": '{"payment_id":"pay_paid"}'}}]}
        return {"role": "assistant", "content": '''{"route":"payment_status",
            "answer":"Paid","citations":[{"document_id":"payout-policy","version":2,
            "quote":"Paid means the payment was completed."}],"payment_status":"paid",
            "proposed_action":null,"needs_human":false}'''}

    def fake_read(*args):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            raise logic.RunDeferred(25)
        return "paid"

    monkeypatch.setattr(logic, "model_response", fake_model)
    monkeypatch.setattr(logic, "read_payment", fake_read)
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:3b")
    try:
        with pytest.raises(logic.RunDeferred):
            logic.process_run(run)
        worker.defer_run(run, 25)
        with tx() as conn:
            saved = conn.execute("SELECT * FROM runs WHERE run_id=%s", (run_id,)).fetchone()
            assert saved["status"] == "queued"
            assert saved["messages"][-1]["tool_calls"][0]["id"] == "call-1"
            # Simulate the delayed claim after its due time.
            resumed = conn.execute(
                """UPDATE runs SET status='running',next_attempt_at=NULL,
                          lease_until=now()+interval '20 seconds'
                   WHERE run_id=%s RETURNING *""", (run_id,),
            ).fetchone()
        logic.process_run(resumed)
        with tx() as conn:
            finished = conn.execute("SELECT status,result FROM runs WHERE run_id=%s",
                                    (run_id,)).fetchone()
        assert finished["status"] == "succeeded"
        assert finished["result"]["payment_status"] == "paid"
        assert call_count == 2 and read_count == 2
    finally:
        with tx() as conn:
            conn.execute("DELETE FROM events WHERE run_id=%s", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id=%s", (run_id,))
