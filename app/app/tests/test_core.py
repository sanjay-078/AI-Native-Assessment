import concurrent.futures
import hashlib
import json
import time
import uuid

import httpx
import pytest

from app.db import connect
from app.logic import RunFailure, parse_tool_call, retrieve, validate_result

BASE = "http://api:8090"
ATLAS = {"Authorization": "Bearer atlas-reviewer-key"}
BOREAL = {"Authorization": "Bearer boreal-reviewer-key"}
BOREAL_READER = {"Authorization": "Bearer boreal-reader-key"}
READER = {"Authorization": "Bearer atlas-reader-key"}


def request(method, path, headers=ATLAS, body=None):
    with httpx.Client(timeout=15) as client:
        return client.request(method, BASE + path, headers=headers, json=body)


def wait_run(run_id):
    for _ in range(250):
        response = request("GET", "/v1/runs/" + run_id)
        if response.json()["status"] in ("succeeded", "failed"):
            return response.json()
        time.sleep(.2)
    raise AssertionError("run did not finish")


def test_retrieval_filters_tenant_and_authority():
    with connect() as conn:
        docs = retrieve(conn, "atlas", "What is the fee?", None)
    assert len(docs) == 1
    assert docs[0]["document_id"] == "fees-policy"
    assert docs[0]["tenant_id"] == "atlas"
    assert "2 percent" in docs[0]["text"]
    assert all(d["authority"] == "policy" and d["status"] == "active" for d in docs)


@pytest.mark.parametrize("function", [
    {"name": "create_followup", "arguments": '{"payment_id":"pay_held"}'},
    {"name": "get_payment_status", "arguments": '{"payment_id":"pay_paid"}'},
    {"name": "get_payment_status", "arguments": '{"payment_id":"pay_held","tenant_id":"boreal"}'},
])
def test_rejects_untrusted_tool_call(function):
    with pytest.raises(RunFailure) as exc:
        parse_tool_call({"tool_calls": [{"id": "x", "function": function}]}, "pay_held")
    assert exc.value.code == "invalid_tool"


def test_result_validation_blocks_paid_action_and_corrects_fake_quote():
    run = {"tenant_id": "atlas", "payment_id": "pay_paid"}
    evidence = [{"document_id": "payout-policy", "version": 2, "text": "Paid means completed."}]
    result = {"route": "payment_status", "answer": "Paid.",
              "citations": [{"document_id": "payout-policy", "version": 2,
                             "quote": "Paid means completed."}],
              "payment_status": "paid", "proposed_action": {
                  "type": "create_followup", "payment_id": "pay_paid", "reason": "No"},
              "needs_human": False}
    checked = validate_result(result, run, evidence, "paid")
    assert checked["proposed_action"] is None
    result["citations"][0]["quote"] = "A guaranteed payout tomorrow."
    checked = validate_result(result, run, evidence, "paid")
    assert checked["citations"][0]["quote"] == "Paid means completed."
    result["citations"][0]["document_id"] = "foreign-policy"
    with pytest.raises(RunFailure):
        validate_result(result, run, evidence, "paid")


def test_missing_and_unsupported_fall_back_safely():
    missing = validate_result({"route": "payment_status", "answer": None,
                               "citations": [{"wrong": "shape"}]},
                              {"payment_id": "pay_typo"}, [{"document_id": "payout-policy"}],
                              "missing")
    assert missing["route"] == "needs_human"
    assert missing["payment_status"] == "missing"
    assert missing["citations"] == [] and missing["proposed_action"] is None
    unsupported = validate_result({"route": "policy_question", "answer": None},
                                  {"payment_id": None, "message": "Unknown policy"}, [], None)
    assert unsupported["route"] == "needs_human"
    assert unsupported["payment_status"] is None
    assert unsupported["needs_human"] is True


@pytest.mark.parametrize("question", [
    "What is the insurance fee policy?",
    "How much do you charge for insurance coverage?",
    "Does the service fee include international transfers?",
    "Can I get a tax waiver on the service fee?",
])
def test_related_document_does_not_support_unmentioned_topic(question):
    evidence = [{"document_id": "fees-policy", "version": 1,
                 "text": "The standard service fee is 2 percent. No document authorizes a fee waiver."}]
    run = {"payment_id": None, "message": question}
    claim = {"route": "policy_question", "answer": "2 percent",
             "citations": [{"document_id": "fees-policy", "version": 1,
                            "quote": "The standard service fee is 2 percent."}],
             "payment_status": None, "proposed_action": None, "needs_human": False}
    result = validate_result(claim, run, evidence, None)
    assert result["route"] == "needs_human"
    assert result["needs_human"] is True
    assert result["citations"] == [] and result["proposed_action"] is None
    assert result["payment_status"] is None


def test_supported_fee_and_model_human_review_are_both_safe():
    evidence = [{"document_id": "fees-policy", "version": 1,
                 "text": "The standard service fee is 2 percent. No document authorizes a fee waiver."}]
    supported = {"route": "policy_question", "answer": "The fee is 2 percent.",
                 "citations": [{"document_id": "fees-policy", "version": 1,
                                "quote": "The standard service fee is 2 percent."}],
                 "payment_status": None, "proposed_action": None, "needs_human": False}
    for question in ("What is the standard service fee?",
                     "Do you have a documented standard percentage charge?"):
        result = validate_result(supported, {"payment_id": None, "message": question},
                                 evidence, None)
        assert result["route"] == "policy_question"
        assert result["citations"][0]["quote"] in evidence[0]["text"]
    # The observed small model chose human review but supplied a null answer.
    result = validate_result({"route": "needs_human", "answer": None,
                              "citations": []},
                             {"payment_id": None, "message": "What is the insurance fee policy?"},
                             evidence, None)
    assert result["route"] == "needs_human" and result["proposed_action"] is None


def test_durable_idempotency_tenant_and_approval():
    key = "test-" + uuid.uuid4().hex
    body = {"request_id": key, "message": "Check this held payout.", "payment_id": "pay_held"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: request("POST", "/v1/runs", body=body), range(6)))
    assert all(r.status_code in (200, 202) for r in responses)
    ids = {r.json()["run_id"] for r in responses}
    assert len(ids) == 1
    run_id = ids.pop()
    assert request("POST", "/v1/runs", body={**body, "message": "Changed"}).status_code == 409
    assert request("GET", "/v1/runs/" + run_id, headers=BOREAL).status_code == 404
    other = request("POST", "/v1/runs", body=body, headers=BOREAL)
    assert other.status_code == 202 and other.json()["run_id"] != run_id
    run = wait_run(run_id)
    assert run["status"] == "succeeded"
    action = run["action"]
    assert action["status"] == "pending_approval"
    frozen = {"type": "create_followup", "tenant_id": "atlas",
              "payment_id": "pay_held",
              "reason": run["result"]["proposed_action"]["reason"]}
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    assert action["payload_hash"] == digest
    approval = {"action_id": action["action_id"], "payload_hash": action["payload_hash"]}
    path = "/v1/runs/" + run_id + "/approve"
    assert request("POST", path, headers=READER, body=approval).status_code == 403
    assert request("POST", path, headers=BOREAL, body=approval).status_code == 404
    assert request("POST", path, headers=BOREAL_READER, body=approval).status_code == 404
    assert request("POST", path, body={**approval, "payload_hash": "0"*64}).status_code == 409
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        approved = list(pool.map(lambda _: request("POST", path, body=approval), range(6)))
    assert all(r.status_code in (200, 202) for r in approved)
    for _ in range(100):
        current = request("GET", "/v1/runs/" + run_id).json()
        if current["action"]["status"] == "completed":
            break
        time.sleep(.2)
    assert current["action"]["status"] == "completed"
    assert current["action"]["receipt"]["action_id"] == action["action_id"]


def test_invalid_input_does_not_poison_queue():
    key = "bad-" + uuid.uuid4().hex
    assert request("POST", "/v1/runs", body={
        "request_id": key, "message": "ok", "payment_id": None
    }).status_code == 400
    assert request("POST", "/v1/runs", body={
        "request_id": key, "message": "ok", "tenant_id": "boreal"
    }).status_code == 400
    valid = request("POST", "/v1/runs", body={
        "request_id": key, "message": "What is the service fee?"
    })
    assert valid.status_code == 202
    assert wait_run(valid.json()["run_id"])["status"] == "succeeded"
