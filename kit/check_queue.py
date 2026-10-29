#!/usr/bin/env python3
"""Queue saturation regression. Run with workers stopped."""

import json
import uuid
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

BASE = "http://localhost:8090"
PREFIX = "queuecheck-" + uuid.uuid4().hex[:12]

auth = json.loads(Path("kit/data/auth.json").read_text())
token = next(
    key for key, value in auth.items()
    if value["tenant_id"] == "atlas"
    and value["role"] == "reviewer"
)

def sql(query):
    return subprocess.check_output(
        [
            "docker", "compose", "exec", "-T", "db",
            "psql", "-v", "ON_ERROR_STOP=1",
            "-U", "assessment_ai", "-d", "assessment_ai",
            "-At", "-c", query
        ],
        text=True
    ).strip()

def submit(i, message="What is the standard service fee?"):
    payload = {
        "request_id": f"{PREFIX}-{i}",
        "message": message
    }

    request = urllib.request.Request(
        BASE + "/v1/runs",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            return r.status, r.headers.get("Retry-After"), json.load(r)
    except urllib.error.HTTPError as error:
        return (
            error.code,
            error.headers.get("Retry-After"),
            json.loads(error.read())
        )

try:
    existing = int(sql(
        "SELECT count(*) FROM runs "
        "WHERE tenant_id='atlas' "
        "AND status IN ('queued','running');"
    ))

    if existing:
        raise RuntimeError(
            f"{existing} existing requests outstanding. "
            "Refusing to interfere with them."
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(18)))

    counts = Counter(status for status, _, _ in results)
    accepted_index = next(
        i for i, result in enumerate(results)
        if result[0] == 202
    )

    replay = submit(accepted_index)
    conflict = submit(accepted_index, "Changed request")

    original_id = results[accepted_index][2]["run_id"]

    assert counts[202] == 16, counts
    assert counts[429] == 2, counts
    assert replay[0] in (200, 202)
    assert replay[2]["run_id"] == original_id
    assert conflict[0] == 409
    assert all(
        retry == "5"
        for status, retry, _ in results
        if status == 429
    )

    report = {
        "submitted": 18,
        "accepted": counts[202],
        "rejected": counts[429],
        "replay_status": replay[0],
        "conflict_status": conflict[0],
        "retry_after": "5",
        "result": "PASS"
    }

    Path("results/queue-check.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )

    print(json.dumps(report, indent=2))
    print("RESULT: PERMANENT QUEUE REGRESSION PASSED")

finally:
    sql(
        "BEGIN;"
        "DELETE FROM events WHERE run_id IN "
        "(SELECT run_id FROM runs WHERE tenant_id='atlas' "
        f"AND request_id LIKE '{PREFIX}-%');"
        "DELETE FROM runs WHERE tenant_id='atlas' "
        f"AND request_id LIKE '{PREFIX}-%';"
        "COMMIT;"
    )
    print("Synthetic test records cleaned up.")
