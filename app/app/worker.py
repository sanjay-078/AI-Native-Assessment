import hashlib
import json
import os
import time
from urllib.parse import quote

import httpx

from .db import event, migrate, tx
from .logic import RunDeferred, RunFailure, action_payload, canonical, process_run


def acquire_run():
    with tx() as conn:
        row = conn.execute(
            """WITH candidate AS (
                 SELECT run_id,status AS previous_status FROM runs
                 WHERE (status='queued' AND
                        (next_attempt_at IS NULL OR next_attempt_at<=now()))
                    OR (status='running' AND lease_until<now())
                 ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
               )
               UPDATE runs AS r SET status='running',
                 started_at=coalesce(r.started_at,now()),
                 lease_until=now()+interval '20 seconds',next_attempt_at=NULL
               FROM candidate WHERE r.run_id=candidate.run_id
               RETURNING r.*,candidate.previous_status"""
        ).fetchone()
        if row:
            event(conn, row["run_id"], row["tenant_id"], "worker_claimed", {
                "recovered": row["previous_status"] == "running",
                "queue_ms": max(0, round((time.time() - row["created_at"].timestamp()) * 1000)),
                "model_calls": row["model_calls"], "read_calls": row["read_calls"],
            })
        return row


def defer_run(run, seconds):
    with tx() as conn:
        row = conn.execute(
            """UPDATE runs SET status='queued',lease_until=NULL,
                      next_attempt_at=now()+(%s * interval '1 second')
               WHERE run_id=%s AND status='running' RETURNING run_id""",
            (seconds, run["run_id"]),
        ).fetchone()
        if row:
            event(conn, run["run_id"], run["tenant_id"], "run_deferred",
                  {"retry_after": seconds})


def fail_run(run, exc):
    with tx() as conn:
        conn.execute(
            """UPDATE runs SET status='failed',error=%s,completed_at=now(),lease_until=NULL
               WHERE run_id=%s AND status='running'""",
            (json.dumps({"code": exc.code, "message": exc.message}), run["run_id"]),
        )
        event(conn, run["run_id"], run["tenant_id"], "run_failed",
              {"code": exc.code, "message": exc.message})


def acquire_action():
    with tx() as conn:
        # Close expired actions that exhausted the reconciliation budget.
        # The external outcome is UNKNOWN, not necessarily unsuccessful.
        expired = conn.execute(
            """UPDATE actions SET status='failed', error=%s,
                       lease_until=NULL, next_attempt_at=NULL
               WHERE status='executing'
                 AND lease_until<now() AND attempts>=9
               RETURNING action_id,run_id,tenant_id,attempts""",
            (json.dumps({
                "code": "external_outcome_unknown",
                "message": "External receipt could not be confirmed; manual reconciliation required"
            }),),
        ).fetchall()

        for item in expired:
            event(conn, item["run_id"], item["tenant_id"], "action_failed", {
                "action_id": item["action_id"],
                "reason": "external_outcome_unknown",
                "attempt": item["attempts"]
            })

        row = conn.execute(
            """WITH candidate AS (
                 SELECT action_id FROM actions
                 WHERE (status='approved' AND attempts<6 AND
                         (next_attempt_at IS NULL OR next_attempt_at<=now()))
                    OR (status='executing' AND lease_until<now() AND attempts<9)
                 ORDER BY approved_at LIMIT 1 FOR UPDATE SKIP LOCKED
               )
               UPDATE actions AS a SET status='executing',attempts=a.attempts+1,
                 lease_until=now()+interval '20 seconds'
               FROM candidate WHERE a.action_id=candidate.action_id
               RETURNING a.*"""
        ).fetchone()
        if row:
            event(conn, row["run_id"], row["tenant_id"], "action_claimed", {
                "action_id": row["action_id"], "attempt": row["attempts"],
                "idempotency_key": row["idempotency_key"],
            })
        return row


def verify_receipt(receipt, action):
    return (
        isinstance(receipt, dict)
        and receipt.get("tenant_id") == action["tenant_id"]
        and receipt.get("payment_id") == action["payment_id"]
        and receipt.get("action_id") == action["action_id"]
        and receipt.get("idempotency_key") == action["idempotency_key"]
        and receipt.get("status") == "created"
        and isinstance(receipt.get("receipt_id"), str)
    )


def execute_action(action):
    frozen = action_payload(action["tenant_id"], {
        "payment_id": action["payment_id"], "reason": action["reason"],
    })
    if action["action_type"] != "create_followup" or hashlib.sha256(
        canonical(frozen).encode()
    ).hexdigest() != action["payload_hash"]:
        raise RunFailure("action_integrity", "Stored action integrity check failed")
    base = os.environ["TOOL_BASE_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["TOOL_API_KEY"]}
    with httpx.Client(timeout=10) as client:
        lookup = client.get(
            base + "/v1/followups/" + quote(action["idempotency_key"]),
            params={"tenant_id": action["tenant_id"]}, headers=headers,
        )
        if lookup.status_code == 200:
            receipt = lookup.json()
            if not verify_receipt(receipt, action):
                raise RunFailure("receipt_mismatch", "Existing receipt did not match action")
            return receipt
        if lookup.status_code != 404:
            raise RunFailure("tool_unavailable", "Could not reconcile action")
        if action["attempts"] > 6:
            raise RunFailure("write_uncertain_exhausted", "Follow-up receipt was not found")
        payment = client.get(
            base + "/v1/payments/" + quote(action["payment_id"]),
            params={"tenant_id": action["tenant_id"]}, headers=headers,
        )
        if payment.status_code != 200:
            raise RunFailure("payment_unverified", "Payment could not be rechecked")
        state = payment.json()
        if (state.get("tenant_id") != action["tenant_id"] or
                state.get("payment_id") != action["payment_id"] or
                state.get("status") != "held"):
            raise RunFailure("payment_not_held", "Payment is no longer held")
        payload = {
            "tenant_id": action["tenant_id"], "payment_id": action["payment_id"],
            "reason": action["reason"], "action_id": action["action_id"],
        }
        response = client.post(
            base + "/v1/followups", json=payload,
            headers={**headers, "Idempotency-Key": action["idempotency_key"]},
        )
        if response.status_code not in (200, 201):
            raise RunFailure("write_rejected", "Follow-up tool rejected the action")
        receipt = response.json()
        if not verify_receipt(receipt, action):
            raise RunFailure("receipt_mismatch", "Follow-up receipt did not match action")
        return receipt


def complete_action(action, receipt):
    with tx() as conn:
        conn.execute(
            """UPDATE actions SET status='completed',receipt=%s,lease_until=NULL,error=NULL
               WHERE action_id=%s""", (json.dumps(receipt), action["action_id"]),
        )
        event(conn, action["run_id"], action["tenant_id"], "action_completed", {
            "action_id": action["action_id"], "idempotency_key": action["idempotency_key"],
            "receipt": receipt,
        })


def action_error(action, exc):
    terminal = exc.code in ("action_integrity", "receipt_mismatch", "payment_not_held",
                            "write_rejected", "write_uncertain_exhausted")
    if action["attempts"] >= 9 and not terminal:
        exc = RunFailure(
            "external_outcome_unknown",
            "External receipt could not be confirmed; manual reconciliation required"
        )
        terminal = True

    reconcile_only = action["attempts"] >= 6 and not terminal
    with tx() as conn:
        conn.execute(
            """UPDATE actions SET status=%s,error=%s,lease_until=NULL,
               next_attempt_at=CASE WHEN %s THEN NULL ELSE now()+interval '1 second' END
               WHERE action_id=%s""",
            ("failed" if terminal else "executing" if reconcile_only else "approved",
             json.dumps({"code": exc.code, "message": exc.message}),
             terminal, action["action_id"]),
        )
        if reconcile_only:
            conn.execute("UPDATE actions SET lease_until=now()+interval '5 seconds' WHERE action_id=%s",
                         (action["action_id"],))
        event(conn, action["run_id"], action["tenant_id"],
              "action_failed" if terminal else "action_retry", {
                  "action_id": action["action_id"], "reason": exc.code,
                  "attempt": action["attempts"],
              })


def work_once():
    action = acquire_action()
    if action:
        try:
            receipt = execute_action(action)
            complete_action(action, receipt)
        except RunFailure as exc:
            action_error(action, exc)
        except (httpx.HTTPError, ValueError):
            action_error(action, RunFailure("tool_transport", "External tool response is uncertain"))
        return True
    run = acquire_run()
    if run:
        try:
            process_run(run)
        except RunDeferred as exc:
            defer_run(run, exc.seconds)
        except RunFailure as exc:
            fail_run(run, exc)
        except Exception:
            fail_run(run, RunFailure("internal_error", "Worker could not complete the run"))
            raise
        return True
    return False


def main():
    migrate()
    while True:
        if not work_once():
            time.sleep(0.2)


if __name__ == "__main__":
    main()
