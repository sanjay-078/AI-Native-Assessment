import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg import DatabaseError

from .db import connect, event, migrate, tx

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
AUTH = {}


@asynccontextmanager
async def lifespan(app):
    migrate()
    AUTH.update(json.loads((Path(os.environ["DATA_DIR"]) / "auth.json").read_text()))
    yield


app = FastAPI(lifespan=lifespan)

from fastapi.staticfiles import StaticFiles

app.mount(
    "/ui",
    StaticFiles(
        directory=str(Path(__file__).parent / "ui"),
        html=True
    ),
    name="assessment-ui"
)


def fail(status, code, message):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


def principal(request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        fail(401, "unauthorized", "Bearer token required")
    auth = AUTH.get(header[7:])
    if auth is None:
        fail(401, "unauthorized", "Unknown credential")
    return auth["tenant_id"], auth["role"]


def no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate key")
        obj[key] = value
    return obj


async def body(request, keys):
    raw = await request.body()
    if len(raw) > 65536:
        fail(413, "body_too_large", "Body exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except (ValueError, UnicodeDecodeError):
        fail(400, "bad_json", "Invalid JSON")
    if not isinstance(value, dict) or set(value) - keys:
        fail(400, "bad_schema", "Unexpected fields")
    return value


def valid_string(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(c) < 32 or ord(c) == 127 for c in value
    )


def run_response(conn, run):
    action = conn.execute(
        "SELECT action_id,payload_hash,status,receipt,error FROM actions WHERE run_id=%s AND tenant_id=%s",
        (run["run_id"], run["tenant_id"]),
    ).fetchone()
    if action and action["receipt"] is None:
        action.pop("receipt")
    return {
        "run_id": run["run_id"], "status": run["status"], "result": run["result"],
        "action": action, "error": run["error"],
    }


@app.get("/healthz")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1 FROM runs LIMIT 1")
        return {"ready": True}
    except DatabaseError:
        fail(503, "not_ready", "Database unavailable")


@app.post("/v1/runs")
async def create(request: Request):
    tenant, _ = principal(request)
    b = await body(request, {"request_id", "message", "payment_id"})
    request_id, message = b.get("request_id"), b.get("message")
    if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
        fail(400, "bad_request_id", "Invalid request_id")
    if not valid_string(message, 12000):
        fail(400, "bad_message", "Invalid message")
    payment_id = b.get("payment_id")
    if "payment_id" in b and (
        not isinstance(payment_id, str) or not ID_RE.fullmatch(payment_id)
    ):
        fail(400, "bad_payment_id", "Invalid payment_id")
    inputs = {"message": message}
    if "payment_id" in b:
        inputs["payment_id"] = payment_id
    fingerprint = hashlib.sha256(
        json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    run_id = "r_" + uuid.uuid4().hex
    try:
        with tx() as conn:
            # Serialize admissions for each tenant across API processes.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), 8091)",
                (tenant,)
            )

            # Replays must remain idempotent even when the queue is full.
            existing = conn.execute(
                "SELECT run_id,status,fingerprint FROM runs "
                "WHERE tenant_id=%s AND request_id=%s",
                (tenant, request_id)
            ).fetchone()

            if existing:
                if existing["fingerprint"] != fingerprint:
                    fail(
                        409,
                        "idempotency_conflict",
                        "request_id was used with different inputs"
                    )
                return {
                    "run_id": existing["run_id"],
                    "status": existing["status"]
                }

            limit = max(
                1,
                int(os.getenv("MAX_PENDING_RUNS_PER_TENANT", "16"))
            )

            outstanding = conn.execute(
                "SELECT count(*) AS total FROM runs "
                "WHERE tenant_id=%s "
                "AND status IN ('queued','running')",
                (tenant,)
            ).fetchone()["total"]

            if outstanding >= limit:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": "5"},
                    content={
                        "code": "queue_full",
                        "message": "Too many outstanding requests. Retry later."
                    }
                )

            inserted = conn.execute(
                """INSERT INTO runs(run_id,tenant_id,request_id,fingerprint,message,payment_id)
                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,request_id) DO NOTHING
                   RETURNING run_id,status""",
                (run_id, tenant, request_id, fingerprint, message, payment_id),
            ).fetchone()
            if inserted:
                event(conn, run_id, tenant, "accepted", {"request_id": request_id})
                return JSONResponse(status_code=202, content=inserted)
            existing = conn.execute(
                "SELECT run_id,status,fingerprint FROM runs WHERE tenant_id=%s AND request_id=%s",
                (tenant, request_id),
            ).fetchone()
            if existing["fingerprint"] != fingerprint:
                fail(409, "idempotency_conflict", "request_id was used with different inputs")
            return {"run_id": existing["run_id"], "status": existing["status"]}
    except DatabaseError:
        return JSONResponse(status_code=503, headers={"Retry-After": "1"},
                            content={"code": "unavailable", "message": "Could not durably accept run"})


def get_authorized(conn, tenant, run_id):
    run = conn.execute(
        "SELECT * FROM runs WHERE run_id=%s AND tenant_id=%s", (run_id, tenant)
    ).fetchone()
    if run is None:
        fail(404, "not_found", "Run not found")
    return run


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, request: Request):
    tenant, _ = principal(request)
    with connect() as conn:
        return run_response(conn, get_authorized(conn, tenant, run_id))


@app.get("/v1/runs/{run_id}/trace")
def trace(run_id: str, request: Request):
    tenant, _ = principal(request)
    with connect() as conn:
        get_authorized(conn, tenant, run_id)
        rows = conn.execute(
            "SELECT sequence,type,at,data FROM events WHERE run_id=%s AND tenant_id=%s ORDER BY sequence",
            (run_id, tenant),
        ).fetchall()
    for row in rows:
        row["at"] = row["at"].isoformat()
    return {"run_id": run_id, "events": rows}


@app.post("/v1/runs/{run_id}/approve")
async def approve(run_id: str, request: Request):
    tenant, role = principal(request)
    b = await body(request, {"action_id", "payload_hash"})
    if set(b) != {"action_id", "payload_hash"} or any(
        not isinstance(v, str) or not v for v in b.values()
    ):
        fail(400, "bad_schema", "action_id and payload_hash required")
    with tx() as conn:
        get_authorized(conn, tenant, run_id)
        if role != "reviewer":
            fail(403, "forbidden", "Reviewer role required")
        action = conn.execute(
            "SELECT * FROM actions WHERE run_id=%s AND tenant_id=%s FOR UPDATE",
            (run_id, tenant),
        ).fetchone()
        if not action or action["action_id"] != b["action_id"] or action["payload_hash"] != b["payload_hash"]:
            fail(409, "action_conflict", "Action identity or payload hash mismatch")
        if action["status"] == "pending_approval":
            actor_id = hashlib.sha256(request.headers["authorization"][7:].encode()).hexdigest()[:16]
            conn.execute(
                """UPDATE actions SET status='approved',approved_by_role='reviewer',
                   approved_by=%s,approved_at=now() WHERE action_id=%s""",
                (actor_id, action["action_id"]),
            )
            event(conn, run_id, tenant, "approved", {
                "action_id": action["action_id"], "role": role, "actor_id": actor_id,
                "payload_hash": action["payload_hash"],
            })
        return JSONResponse(status_code=202 if action["status"] != "completed" else 200,
                            content={"action_id": action["action_id"], "status": (
                                "approved" if action["status"] == "pending_approval" else action["status"]
                            )})
