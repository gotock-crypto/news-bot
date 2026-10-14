import threading
import time

import newsbot.llm.router as router


class FakeResponse:
    status_code = 200
    headers = {}

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": "ok"
                    }
                }
            ]
        }

    def raise_for_status(self):
        return None


def test_router_does_not_hold_lock_during_http(monkeypatch):
    entered = []
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def fake_post(*args, **kwargs):
        entered.append(threading.current_thread().name)

        if len(entered) == 1:
            first_entered.set()
            release.wait(timeout=2)
        else:
            second_entered.set()

        return FakeResponse()

    monkeypatch.setattr(router.requests, "post", fake_post)

    router._LAST_REQUEST_AT = 0.0
    router._GROQ_COOLDOWN_UNTIL = {}

    kwargs = dict(
        provider="mistral",
        key="test-key",
        model="test-model",
        messages=[{"role": "user", "content": "test"}],
        max_tokens=10,
        temperature=0.0,
        timeout=2,
        min_interval=0,
        groq_cooldown=30,
    )

    errors = []

    def worker():
        try:
            router.request(**kwargs)
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=worker, name="router-first")
    second = threading.Thread(target=worker, name="router-second")

    first.start()
    assert first_entered.wait(timeout=1)

    second.start()

    # The second HTTP request must be able to enter requests.post()
    # while the first HTTP request is still blocked there.
    assert second_entered.wait(timeout=1)

    release.set()

    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(entered) == 2

def test_groq_cooldown_is_key_specific():
    router._GROQ_COOLDOWN_UNTIL = {
        "groq-key-1": time.monotonic() + 60,
    }

    assert not router.groq_available("groq-key-1")
    assert router.groq_available("groq-key-2")

    router._GROQ_COOLDOWN_UNTIL = {}


class UsageResponse(FakeResponse):
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {}
        self._payload = payload
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        return self._payload


def test_router_logs_openai_usage(monkeypatch, caplog):
    response = UsageResponse({
        "choices": [
            {
                "message": {
                    "content": "ok"
                }
            }
        ],
        "usage": {
            "prompt_tokens": 123,
            "completion_tokens": 17,
            "total_tokens": 140,
        },
    })

    monkeypatch.setattr(router.requests, "post", lambda *args, **kwargs: response)

    with caplog.at_level("INFO", logger="newsbot.llm.router"):
        raw = router.request(
            provider="mistral",
            key="test-key",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=10,
            temperature=0.0,
            timeout=2,
            min_interval=0,
            groq_cooldown=30,
        )

    assert raw == "ok"
    assert response.json_calls == 1
    assert (
        "LLM_USAGE provider=mistral model=test-model "
        "input_tokens=123 output_tokens=17 total_tokens=140"
    ) in caplog.text


def test_router_logs_gemini_usage(monkeypatch, caplog):
    response = UsageResponse({
        "candidates": [{
            "content": {
                "parts": [{"text": "{}"}]
            }
        }],
        "usageMetadata": {
            "promptTokenCount": 321,
            "candidatesTokenCount": 29,
            "totalTokenCount": 350,
        },
    })

    monkeypatch.setattr(router.requests, "post", lambda *args, **kwargs: response)

    with caplog.at_level("INFO", logger="newsbot.llm.router"):
        raw = router.request(
            provider="gemini",
            key="test-key",
            model="gemini-3.6-flash",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=100,
            temperature=0.0,
            timeout=2,
            min_interval=0,
            groq_cooldown=30,
        )

    assert raw == "{}"
    assert response.json_calls == 1
    assert (
        "LLM_USAGE provider=gemini model=gemini-3.6-flash "
        "input_tokens=321 output_tokens=29 total_tokens=350"
    ) in caplog.text
