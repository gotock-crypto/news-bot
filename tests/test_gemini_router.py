import json

import newsbot.llm.router as router


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_gemini_request_uses_generate_content_and_parses_json(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse({
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"title":"Test","text":"Body","category":"news","priority":1,"confidence":0.9,"hashtags":["a","b","c"],"publish":true,"is_digest":false}'
                    }]
                },
                "finishReason": "STOP",
            }]
        })

    monkeypatch.setattr(router.requests, "post", fake_post)

    raw = router.request(
        provider="gemini",
        key="secret-key",
        model="gemini-3.6-flash",
        messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "INPUT"},
        ],
        max_tokens=900,
        temperature=0.25,
        timeout=10,
        min_interval=0,
        groq_cooldown=30,
    )

    assert json.loads(raw)["title"] == "Test"
    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-3.6-flash:generateContent?key=secret-key"
    )

    payload = captured["kwargs"]["json"]
    assert payload["contents"] == [{
        "role": "user",
        "parts": [{"text": "INPUT"}],
    }]
    assert payload["systemInstruction"] == {
        "parts": [{"text": "SYSTEM"}],
    }
    assert payload["generationConfig"]["maxOutputTokens"] == 900
    assert payload["generationConfig"]["temperature"] == 0.25
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "minimal",
    }

    assert captured["kwargs"]["headers"] == {
        "Content-Type": "application/json",
    }


def test_gemini_maps_assistant_role_to_model(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["kwargs"] = kwargs
        return FakeResponse({
            "candidates": [{
                "content": {
                    "parts": [{"text": "{}"}]
                }
            }]
        })

    monkeypatch.setattr(router.requests, "post", fake_post)

    router.request(
        provider="gemini",
        key="key",
        model="gemini-3.6-flash",
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
        max_tokens=100,
        temperature=0,
        timeout=10,
        min_interval=0,
        groq_cooldown=30,
    )

    assert captured["kwargs"]["json"]["contents"] == [
        {"role": "user", "parts": [{"text": "one"}]},
        {"role": "model", "parts": [{"text": "two"}]},
        {"role": "user", "parts": [{"text": "three"}]},
    ]


def test_gemini_empty_parts_are_rejected(monkeypatch):
    def fake_post(url, **kwargs):
        return FakeResponse({
            "candidates": [{
                "content": {
                    "parts": []
                }
            }]
        })

    monkeypatch.setattr(router.requests, "post", fake_post)

    try:
        router.request(
            provider="gemini",
            key="key",
            model="gemini-3.6-flash",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=100,
            temperature=0,
            timeout=10,
            min_interval=0,
            groq_cooldown=30,
        )
    except RuntimeError as exc:
        assert "empty content" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")


def test_gemini_http_error_is_propagated(monkeypatch):
    def fake_post(url, **kwargs):
        return FakeResponse({"error": {"message": "bad"}}, status_code=400)

    monkeypatch.setattr(router.requests, "post", fake_post)

    try:
        router.request(
            provider="gemini",
            key="key",
            model="gemini-3.6-flash",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=100,
            temperature=0,
            timeout=10,
            min_interval=0,
            groq_cooldown=30,
        )
    except RuntimeError as exc:
        assert "HTTP 400" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")


def test_task_routing_includes_gemini_last():
    from tests.test_llm_task_routing import run_task

    assert run_task("editorial", {"groq", "mistral"}) == [
        "groq",
        "mistral",
        "gemini",
    ]

    assert run_task("story_classify", {"mistral", "groq"}) == [
        "mistral",
        "groq",
        "gemini",
    ]

    assert run_task("digest_adjudication", {"gigachat", "mistral"}) == [
        "gigachat",
        "mistral",
        "gemini",
    ]
