from types import SimpleNamespace
from newsbot.llm.adapter import LLM


def s():
    return SimpleNamespace(max_post_length=800)


def test_normalize_priority_is_0_to_10():
    x = LLM(s())._normalize({"publish":True,"title":"T","text":"X","priority":99,"confidence":.9})
    assert x["priority"] == 10


def test_normalize_digest_forces_no_publish():
    x = LLM(s())._normalize({"publish":True,"is_digest":True,"title":"T","text":"X"})
    assert x["publish"] is False
    assert x["title"] == ""


def test_analytics_guard():
    llm = LLM(s())
    assert llm._is_non_news_text("Аналитический разбор ситуации")
    assert llm._is_non_news_text("Что это значит для экономики")
    assert not llm._is_non_news_text("Правительство приняло решение")


def test_parse_json_with_prefix():
    data = LLM._parse("Ответ:\n{\"publish\":true,\"title\":\"x\"}\nГотово")
    assert data["publish"] is True
