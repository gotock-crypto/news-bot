import asyncio
from types import SimpleNamespace

from newsbot.llm.adapter import LLM


class RoutingLLM(LLM):
    def __init__(
        self,
        failing=None,
        groq_keys=None,
        mistral_keys=None,
    ):
        groq_keys = tuple(groq_keys or ("groq-key",))
        mistral_keys = tuple(mistral_keys or ("mistral-key",))

        super().__init__(
            SimpleNamespace(
                groq_key=groq_keys[0],
                groq_keys=groq_keys,
                groq_model="groq-model",
                mistral_key=mistral_keys[0],
                mistral_keys=mistral_keys,
                mistral_model="mistral-model",
                gigachat_key="gigachat-key",
                gigachat_model="gigachat-model",
                gemini_key="gemini-key",
                gemini_model="gemini-model",
                llm_min_interval_seconds=0,
            )
        )
        self.calls = []
        self.call_keys = []
        self.failing = set(failing or ())

    def _parse(self, raw):
        return {}

    def _call_provider_with_system(
        self, name, key, model, tokens, temp, prompt, system_prompt
    ):
        self.calls.append(name)
        self.call_keys.append(key)
        if key in self.failing or name in self.failing:
            raise RuntimeError(f"{name} failed")
        return "{}"


def run_task(task, failing=None):
    llm = RoutingLLM(failing=failing)
    asyncio.run(
        llm._request_with_fallback(
            "test",
            task=task,
            max_tokens=100,
            temperature=0,
        )
    )
    return llm.calls


def test_editorial_uses_groq_first():
    assert run_task("editorial") == ["groq"]


def test_editorial_falls_back_to_mistral():
    assert run_task("editorial", {"groq"}) == ["groq", "mistral"]


def test_story_classify_uses_mistral_first():
    assert run_task("story_classify") == ["mistral"]


def test_story_classify_falls_back_to_groq():
    assert run_task("story_classify", {"mistral"}) == ["mistral", "groq"]


def test_digest_adjudication_uses_gigachat_first():
    assert run_task("digest_adjudication") == ["gigachat"]


def test_digest_adjudication_falls_back_to_mistral():
    assert run_task("digest_adjudication", {"gigachat"}) == ["gigachat", "mistral"]


def test_editorial_compact_uses_only_mistral():
    assert run_task("editorial_compact") == ["mistral"]


def test_editorial_compact_falls_back_to_gemini():
    assert run_task("editorial_compact", {"mistral"}) == ["mistral", "gemini"]


def test_update_check_falls_back_to_groq():
    assert run_task("update_check", {"mistral"}) == ["mistral", "groq"]


def test_update_compose_uses_only_mistral():
    assert run_task("update_compose") == ["mistral"]


def test_unknown_task_has_conservative_fallback_order():
    assert run_task("unknown_task") == ["mistral"]


def test_unknown_task_falls_back_to_groq():
    assert run_task("unknown_task", {"mistral"}) == ["mistral", "groq"]


def test_editorial_uses_second_groq_key_before_mistral():
    llm = RoutingLLM(
        failing={"groq-key-1"},
        groq_keys=("groq-key-1", "groq-key-2"),
        mistral_keys=("mistral-key-1",),
    )
    asyncio.run(
        llm._request_with_fallback(
            "test",
            task="editorial",
            max_tokens=100,
            temperature=0,
        )
    )
    assert llm.calls == ["groq", "groq"]
    assert llm.call_keys == ["groq-key-1", "groq-key-2"]


def test_editorial_uses_mistral_after_all_groq_keys_fail():
    llm = RoutingLLM(
        failing={"groq-key-1", "groq-key-2"},
        groq_keys=("groq-key-1", "groq-key-2"),
        mistral_keys=("mistral-key-1",),
    )
    asyncio.run(
        llm._request_with_fallback(
            "test",
            task="editorial",
            max_tokens=100,
            temperature=0,
        )
    )
    assert llm.calls == ["groq", "groq", "mistral"]
    assert llm.call_keys == [
        "groq-key-1",
        "groq-key-2",
        "mistral-key-1",
    ]


def test_story_classify_uses_second_mistral_key_before_groq():
    llm = RoutingLLM(
        failing={"mistral-key-1"},
        groq_keys=("groq-key-1",),
        mistral_keys=("mistral-key-1", "mistral-key-2"),
    )
    asyncio.run(
        llm._request_with_fallback(
            "test",
            task="story_classify",
            max_tokens=100,
            temperature=0,
        )
    )
    assert llm.calls == ["mistral", "mistral"]
    assert llm.call_keys == ["mistral-key-1", "mistral-key-2"]


def test_story_classify_uses_groq_after_all_mistral_keys_fail():
    llm = RoutingLLM(
        failing={"mistral-key-1", "mistral-key-2"},
        groq_keys=("groq-key-1",),
        mistral_keys=("mistral-key-1", "mistral-key-2"),
    )
    asyncio.run(
        llm._request_with_fallback(
            "test",
            task="story_classify",
            max_tokens=100,
            temperature=0,
        )
    )
    assert llm.calls == ["mistral", "mistral", "groq"]
    assert llm.call_keys == [
        "mistral-key-1",
        "mistral-key-2",
        "groq-key-1",
    ]

def test_update_check_rejects_non_object_result():
    class UpdateCheckLLM(LLM):
        def __init__(self):
            super().__init__(SimpleNamespace(llm_min_interval_seconds=0))

        async def _request_with_fallback(self, *args, **kwargs):
            return '[{"score": 95}]'

    llm = UpdateCheckLLM()
    import pytest
    with pytest.raises(ValueError, match="Update classification result must be an object"):
        asyncio.run(llm.classify_update("old", "old text", {"source": "src", "text": "new"}))


def test_update_compose_rejects_non_object_result():
    class UpdateComposeLLM(LLM):
        def __init__(self):
            super().__init__(SimpleNamespace(llm_min_interval_seconds=0))

        async def _request_with_fallback(self, *args, **kwargs):
            return '[{"publish": true, "text": "bad"}]'

    llm = UpdateComposeLLM()
    import pytest
    with pytest.raises(ValueError, match="Update composition result must be an object"):
        asyncio.run(llm.compose_event_update("old", "old text", {"source": "src", "text": "new"}))
