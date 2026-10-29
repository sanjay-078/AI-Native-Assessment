import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


@contextmanager
def tx():
    with connect() as conn:
        with conn.transaction():
            yield conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 tenant_id text NOT NULL, document_id text NOT NULL, version integer NOT NULL,
 status text NOT NULL, authority text NOT NULL, title text NOT NULL, body text NOT NULL,
 PRIMARY KEY (tenant_id, document_id, version)
);
CREATE INDEX IF NOT EXISTS documents_active ON documents(tenant_id, document_id)
 WHERE status='active' AND authority='policy';
CREATE TABLE IF NOT EXISTS runs (
 run_id text PRIMARY KEY, tenant_id text NOT NULL, request_id text NOT NULL,
 fingerprint text NOT NULL, message text NOT NULL, payment_id text,
 status text NOT NULL DEFAULT 'queued', result jsonb, error jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz,
 completed_at timestamptz, lease_until timestamptz, next_attempt_at timestamptz,
 model_calls integer NOT NULL DEFAULT 0, read_calls integer NOT NULL DEFAULT 0,
 repairs integer NOT NULL DEFAULT 0, messages jsonb, evidence jsonb,
 observed_payment_status text,
 UNIQUE (tenant_id, request_id)
);
CREATE INDEX IF NOT EXISTS runs_work ON runs(status, lease_until, created_at);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;
CREATE INDEX IF NOT EXISTS runs_deferred ON runs(next_attempt_at, created_at)
 WHERE status='queued';
CREATE TABLE IF NOT EXISTS actions (
 action_id text PRIMARY KEY, run_id text NOT NULL UNIQUE REFERENCES runs(run_id),
 tenant_id text NOT NULL, action_type text NOT NULL, payment_id text NOT NULL,
 reason text NOT NULL, payload_hash text NOT NULL, idempotency_key text NOT NULL UNIQUE,
 status text NOT NULL DEFAULT 'pending_approval', approved_by_role text,
 approved_by text,
 approved_at timestamptz, lease_until timestamptz, attempts integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz, receipt jsonb, error jsonb
);
CREATE INDEX IF NOT EXISTS actions_work ON actions(status, lease_until, next_attempt_at);
ALTER TABLE actions ADD COLUMN IF NOT EXISTS approved_by text;
CREATE TABLE IF NOT EXISTS events (
 sequence bigserial PRIMARY KEY, run_id text NOT NULL REFERENCES runs(run_id),
 tenant_id text NOT NULL, type text NOT NULL, at timestamptz NOT NULL DEFAULT now(),
 data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);
"""


def migrate():
    with tx() as conn:
        # Prevent simultaneous schema migrations.
        # Transaction lock is released automatically.
        conn.execute(
            "SELECT pg_advisory_xact_lock(20261029, 8090)"
        )
        conn.execute(SCHEMA)


def event(conn, run_id, tenant_id, kind, data=None):
    conn.execute(
        "INSERT INTO events(run_id,tenant_id,type,data) VALUES(%s,%s,%s,%s)",
        (run_id, tenant_id, kind, json.dumps(data or {})),
    )
