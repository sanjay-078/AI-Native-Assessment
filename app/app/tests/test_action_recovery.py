import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import worker
from app.logic import RunFailure, action_payload, canonical


def test_unknown_external_outcome_is_bounded(monkeypatch):
    conn = MagicMock()

    @contextmanager
    def fake_tx():
        yield conn

    monkeypatch.setattr(worker, "tx", fake_tx)
    events = MagicMock()
    monkeypatch.setattr(worker, "event", events)

    action = {
        "action_id": "a_test",
        "run_id": "r_test",
        "tenant_id": "atlas",
        "attempts": 9,
    }

    worker.action_error(
        action,
        RunFailure("tool_transport", "Tool unavailable")
    )

    sql, params = conn.execute.call_args.args

    assert params[0] == "failed"
    assert json.loads(params[1])["code"] == (
        "external_outcome_unknown"
    )
    assert params[2] is True
    assert events.call_args.args[3] == "action_failed"


def test_reconciliation_cannot_issue_new_write(monkeypatch):
    monkeypatch.setenv("TOOL_BASE_URL", "http://fake-gateway")
    monkeypatch.setenv("TOOL_API_KEY", "test")

    proposal = {
        "payment_id": "pay_held",
        "reason": "Review required"
    }

    frozen = action_payload("atlas", proposal)
    digest = hashlib.sha256(
        canonical(frozen).encode()
    ).hexdigest()

    action = {
        "action_id": "a_test",
        "tenant_id": "atlas",
        "payment_id": "pay_held",
        "reason": proposal["reason"],
        "action_type": "create_followup",
        "payload_hash": digest,
        "idempotency_key": "a_test",
        "attempts": 7
    }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=404)

        def post(self, *args, **kwargs):
            raise AssertionError(
                "External write must not be repeated"
            )

    monkeypatch.setattr(worker.httpx, "Client", FakeClient)

    with pytest.raises(RunFailure) as error:
        worker.execute_action(action)

    assert error.value.code == "write_uncertain_exhausted"


def test_expired_action_limit(monkeypatch):
    conn = MagicMock()

    expired = MagicMock()
    expired.fetchall.return_value = [{
        "action_id": "a_expired",
        "run_id": "r_expired",
        "tenant_id": "atlas",
        "attempts": 9
    }]

    next_action = MagicMock()
    next_action.fetchone.return_value = None

    conn.execute.side_effect = [expired, next_action]

    @contextmanager
    def fake_tx():
        yield conn

    monkeypatch.setattr(worker, "tx", fake_tx)

    events = MagicMock()
    monkeypatch.setattr(worker, "event", events)

    result = worker.acquire_action()

    assert result is None

    queries = [
        call.args[0] for call in conn.execute.call_args_list
    ]

    assert "attempts>=9" in queries[0]
    assert "attempts<9" in queries[1]

    assert events.call_args.args[3] == "action_failed"
    assert events.call_args.args[4]["reason"] == (
        "external_outcome_unknown"
    )
