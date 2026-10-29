
import json

import pytest

from app.logic import RunFailure, validate_result


EVIDENCE = [{
    "document_id": "payout-policy",
    "version": 2,
    "text": (
        "Held means verification is outstanding; "
        "do not promise a settlement date."
    ),
}]

RUN = {
    "tenant_id": "atlas",
    "payment_id": "pay_held",
    "message": "Check this held payment.",
}


def model_result(proposal):
    return {
        "route": "payment_status",
        "answer": "Payment is held.",
        "citations": [{
            "document_id": "payout-policy",
            "version": 2,
            "quote": EVIDENCE[0]["text"],
        }],
        "payment_status": "held",
        "proposed_action": proposal,
        "needs_human": True,
    }


def test_real_model_json_encoded_proposal():
    proposal = json.dumps({
        "type": "create_followup",
        "payment_id": "pay_held",
        "reason": "Verification is outstanding",
    })

    result = validate_result(
        model_result(proposal), RUN, EVIDENCE, "held"
    )

    assert result["route"] == "payment_status"
    assert result["payment_status"] == "held"
    assert result["needs_human"] is False
    assert isinstance(result["proposed_action"], dict)
    assert result["proposed_action"]["payment_id"] == "pay_held"
    assert result["citations"][0]["document_id"] == "payout-policy"


@pytest.mark.parametrize("bad", [
    "not json",
    "[]",
    json.dumps({
        "type": "create_followup",
        "payment_id": "pay_paid",
        "reason": "Wrong payment",
    }),
    json.dumps({
        "type": "create_followup",
        "payment_id": "pay_held",
        "reason": "Review",
        "tenant_id": "boreal",
    }),
    json.dumps({
        "type": "transfer_money",
        "payment_id": "pay_held",
        "reason": "Unauthorized action",
    }),
])
def test_invalid_string_proposals_still_rejected(bad):
    with pytest.raises(RunFailure) as error:
        validate_result(
            model_result(bad), RUN, EVIDENCE, "held"
        )

    assert error.value.code == "invalid_action"


def test_regular_object_still_accepted():
    proposal = {
        "type": "create_followup",
        "payment_id": "pay_held",
        "reason": "Verification is outstanding",
    }

    result = validate_result(
        model_result(proposal), RUN, EVIDENCE, "held"
    )

    assert result["proposed_action"] == proposal
    assert result["needs_human"] is False
