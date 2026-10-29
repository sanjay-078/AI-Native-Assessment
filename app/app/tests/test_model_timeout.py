import httpx
from unittest.mock import patch
from app import logic


def test_model_timeout_retries_within_budget():
    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            FakeClient.calls += 1

            if FakeClient.calls == 1:
                raise httpx.ReadTimeout("Simulated timeout")

            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": '{"ok":true}'
                        }
                    }]
                },
                request=httpx.Request("POST", url)
            )

    run = {
        "run_id": "timeout-unit-test",
        "tenant_id": "atlas",
        "payment_id": None
    }

    messages = [
        {"role": "user", "content": "Test"}
    ]

    with patch.dict(logic.os.environ, {
        "LLM_MODEL": "qwen2.5:3b",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "LLM_API_KEY": "test"
    }):
        with patch.object(
            logic, "checkpoint", side_effect=[1, 2]
        ) as checkpoint:
            with patch.object(
                logic, "remaining", return_value=40
            ):
                with patch.object(
                    logic, "trace_event"
                ) as trace:
                    with patch.object(
                        logic.httpx, "Client", FakeClient
                    ):
                        first = logic.model_response(run, messages)
                        second = logic.model_response(run, messages)

    assert first is None
    assert second["content"] == '{"ok":true}'
    assert checkpoint.call_count == 2

    event_names = [
        call.args[1]
        for call in trace.call_args_list
    ]

    assert "model_retry" in event_names
    assert "model_attempt" in event_names
