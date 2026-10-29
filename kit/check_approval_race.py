#!/usr/bin/env python3
"""Concurrent synthetic approval and external idempotency check."""

import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

API = "http://localhost:8090"
KIT = "http://localhost:8091"

auth = json.loads(Path("kit/data/auth.json").read_text())
token = next(
    k for k, v in auth.items()
    if v["tenant_id"] == "atlas" and v["role"] == "reviewer"
)


def request(base, method, path, token, body=None):
    headers = {"Authorization": "Bearer " + token}
    data = None

    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    req = urllib.request.Request(
        base + path, data=data, headers=headers, method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def api(method, path, body=None):
    return request(API, method, path, token, body)


def wait_for_run(run_id, seconds=120):
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        _, run = api("GET", "/v1/runs/" + run_id)

        if run["status"] in ("succeeded", "failed"):
            return run

        time.sleep(1)

    raise AssertionError("Run completion timed out")


def wait_for_action(run_id, seconds=90):
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        _, run = api("GET", "/v1/runs/" + run_id)
        action = run.get("action") or {}

        if action.get("status") in ("completed", "failed"):
            return run

        time.sleep(1)

    raise AssertionError("Action completion timed out")


print("========== CREATE HELD PAYMENT ==========")

status, created = api("POST", "/v1/runs", {
    "request_id": "racecheck-" + uuid.uuid4().hex,
    "message": "Check held payment and propose a follow-up.",
    "payment_id": "pay_held"
})

assert status == 202, (status, created)

run_id = created["run_id"]
run = wait_for_run(run_id)

assert run["status"] == "succeeded", run.get("error")
assert run["result"]["payment_status"] == "held"

original_result = run["result"]
action = run["action"]

assert action is not None
assert action["status"] == "pending_approval"

print("Run:", run_id)
print("Action:", action["action_id"])

print("========== SEND EIGHT APPROVALS ==========")

payload = {
    "action_id": action["action_id"],
    "payload_hash": action["payload_hash"]
}

with ThreadPoolExecutor(max_workers=8) as executor:
    responses = list(executor.map(
        lambda _: api(
            "POST",
            "/v1/runs/" + run_id + "/approve",
            payload
        ),
        range(8)
    ))

codes = Counter(code for code, _ in responses)

assert all(code in (200, 202) for code, _ in responses)
assert all(
    result.get("action_id") == action["action_id"]
    for _, result in responses
)

print("Approval responses:", dict(codes))

print("========== VERIFY COMPLETION ==========")

final = wait_for_action(run_id)
completed = final["action"]

assert completed["status"] == "completed"
assert final["result"] == original_result

_, trace = api("GET", "/v1/runs/" + run_id + "/trace")
events = Counter(e["type"] for e in trace["events"])

assert events["approved"] == 1
assert events["action_completed"] == 1

print("Approved events:", events["approved"])
print("Completed events:", events["action_completed"])

print("========== INDEPENDENT GATEWAY AUDIT ==========")

audit_status, audit = request(
    KIT, "GET", "/__admin/audit", "kit-admin-token"
)

assert audit_status == 200

matches = [
    receipt for receipt in audit["receipts"]
    if receipt.get("action_id") == action["action_id"]
]

assert len(matches) == 1, (
    "Expected exactly one committed external follow-up"
)

receipt = matches[0]

assert receipt == completed["receipt"]
assert receipt["idempotency_key"] == action["action_id"]
assert receipt["status"] == "created"

print("External receipts:", len(matches))

print("========== APPROVAL REPLAY ==========")

replay_code, replay = api(
    "POST", "/v1/runs/" + run_id + "/approve", payload
)

assert replay_code in (200, 202)
assert replay["action_id"] == action["action_id"]

report = {
    "run_id": run_id,
    "action_id": action["action_id"],
    "approval_requests": 8,
    "response_codes": dict(codes),
    "approval_events": events["approved"],
    "completion_events": events["action_completed"],
    "external_receipts": len(matches),
    "replay_status": replay_code,
    "immutable_result": True,
    "result": "PASS"
}

Path("results/approval-race-check.json").write_text(
    json.dumps(report, indent=2) + "\n"
)

print(json.dumps(report, indent=2))
print("RESULT: PERMANENT APPROVAL RACE TEST PASSED")
