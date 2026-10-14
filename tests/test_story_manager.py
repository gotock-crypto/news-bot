import asyncio
from types import SimpleNamespace

from newsbot.db import DB
from newsbot.core.pipeline import Pipeline


class StoryLLM:
    def __init__(self, result):
        self.result = result

    async def edit(self, sources):
        return {"publish": True, "title": "Путин в Казахстане", "text": "Путин прибыл в Казахстан.",
                "category": "politics", "priority": 5, "confidence": .95, "reason": "", "hashtags": []}

    async def classify_story(self, *args):
        return self.result

    async def classify_update(self, previous_title, previous_text, new_source, known_facts=None):
        return {"status": "ok", "score": 95, "reason": "new fact"}


class Max:
    def __init__(self): self.calls=[]
    async def publish(self, title, text, *args, **kwargs):
        self.calls.append((title, text, kwargs)); return "max-new"


def settings():
    return SimpleNamespace(auto_publish=True, min_priority_publish=0, min_confidence_publish=.8,
                           dedup_hours=24, story_hours=24, story_candidate_limit=40,
                           story_semantic_candidates=20, groq_key="", gigachat_key="", mistral_key="")


def test_story_fact_is_persisted(tmp_path):
    db = DB(tmp_path / "db.sqlite")
    m = db.insert_message("src", 1, db.now(), "Путин прибыл в Казахстан")
    eid = db.create_event("Путин в Казахстане", "politics", 5, .95, m)
    db.add_story_fact(eid, "Путин прибыл в Казахстан", m)
    facts = db.story_facts(eid)
    assert len(facts) == 1
    assert facts[0]["fact_text"] == "Путин прибыл в Казахстан"


def test_pipeline_update_uses_only_new_fact(tmp_path):
    db = DB(tmp_path / "db.sqlite")
    m = db.insert_message("src", 1, db.now(), "Путин прибыл в Казахстан")
    eid = db.create_event("Путин в Казахстане", "politics", 5, .95, m)
    db.add_story_fact(eid, "Путин прибыл в Казахстан", m)
    aid = db.add_article(eid, m, "NEW", "Путин в Казахстане", "Путин прибыл в Казахстан.", .95)
    db.mark_published(eid, aid, "parent")
    m2 = db.insert_message("src", 2, db.now(), "Путин заявил о готовности увеличить поставки газа")
    result = {"relation":"update", "story_id":eid, "confidence":.95,
              "new_facts":["Путин заявил о готовности увеличить поставки газа"],
              "update_title":"Путин сделал заявление о поставках газа",
              "update_text":"Путин заявил о готовности увеличить поставки газа.", "reason":"new fact"}
    llm = StoryLLM(result)
    maxc = Max()
    pipe = Pipeline(settings(), db, llm, maxc)
    asyncio.run(pipe.handle(m2, "src"))
    assert len(maxc.calls) == 1
    assert "увеличить поставки газа" in maxc.calls[0][1]
    assert "прибыл в Казахстан" not in maxc.calls[0][1]
    assert len(db.story_facts(eid)) == 2


class RetrievalDB:
    """Minimal DB double for StoryManager retrieval tests."""

    def __init__(self, stories):
        self.stories = stories

    def story_candidates(self, hours=24, limit=40):
        return self.stories

    def story_facts(self, event_id, limit=8):
        return []

    def story_messages(self, event_id, limit=4):
        return []


def _story(event_id, title):
    return {
        "event_id": event_id,
        "title": title,
        "created_at": "2026-09-05T00:00:00",
    }


def test_identity_retrieval_includes_named_story():
    from newsbot.core.story_manager import StoryManager

    target = _story(
        317,
        "Скончался актер и основатель карате в СССР Тадеуш Касьянов",
    )

    distractors = [
        _story(
            i,
            f"Умер заслуженный тренер России по карате Сергей Иванов {i}",
        )
        for i in range(250, 257)
    ]

    s = SimpleNamespace(
        story_hours=24,
        story_candidate_limit=40,
        story_semantic_candidates=2,
        story_identity_candidates=5,
    )

    db = RetrievalDB(distractors + [target])
    manager = StoryManager(s, db, StoryLLM({}))

    rows = manager._candidate_rows(
        "Умер заслуженный тренер России по карате Тадеуш Касьянов",
        "Умер заслуженный тренер России по карате Тадеуш Касьянов.",
    )

    assert 317 in {int(row["event_id"]) for row in rows}


def test_new_guard_retrieves_story_outside_semantic_shortlist():
    from newsbot.core.story_manager import StoryManager

    target = _story(
        317,
        "Скончался актер и основатель карате в СССР Тадеуш Касьянов",
    )

    distractors = [
        _story(
            i,
            f"Умер заслуженный тренер России по карате Сергей Иванов {i}",
        )
        for i in range(250, 257)
    ]

    settings = SimpleNamespace(
        story_hours=24,
        story_candidate_limit=40,
        story_semantic_candidates=2,
        story_identity_candidates=1,
        story_new_guard_candidates=10,
    )

    db = RetrievalDB(distractors + [target])
    manager = StoryManager(settings, db, StoryLLM({}))

    title = "Умер заслуженный тренер России по карате Тадеуш Касьянов"
    body = "Умер заслуженный тренер России по карате Тадеуш Касьянов."

    from newsbot.core.dedup import similarity

    incoming = f"{title}\n{body}"

    ranked = []
    for row in db.story_candidates():
        corpus = manager._story_corpus(
            int(row["event_id"]),
            row,
        )
        ranked.append(
            (
                similarity(incoming, corpus),
                int(row["event_id"]),
            )
        )

    ranked.sort(
        key=lambda x: (x[0], x[1]),
        reverse=True,
    )

    semantic_ids = {
        event_id
        for _, event_id in ranked[
            :settings.story_semantic_candidates
        ]
    }

    guard_candidates = manager._guard_candidate_rows(
        title,
        body,
    )
    guard_ids = {
        candidate_id
        for _, _, candidate_id, _ in guard_candidates
    }

    assert 317 not in semantic_ids
    assert 317 in guard_ids



class GuardLLM:
    def __init__(self):
        self.calls = []

    async def classify_story(self, context):
        self.calls.append(context)

        # Initial classification deliberately reproduces the production failure:
        # the model incorrectly calls the incoming death report NEW.
        if len(self.calls) == 1:
            return {
                "relation": "new",
                "story_id": 0,
                "confidence": 1.0,
                "new_facts": ["Умер Тадеуш Касьянов"],
                "update_title": "",
                "update_text": "",
                "reason": "Предварительно принято за новую историю",
            }

        # The NEW guard must eventually inspect Story 317.
        if "STORY_ID: 317" in context:
            return {
                "relation": "duplicate",
                "story_id": 317,
                "confidence": 0.99,
                "new_facts": [],
                "update_title": "",
                "update_text": "",
                "reason": "Это то же сообщение о смерти Тадеуша Касьянова",
            }

        return {
            "relation": "new",
            "story_id": 0,
            "confidence": 0.9,
            "new_facts": [],
            "update_title": "",
            "update_text": "",
            "reason": "Другой эпизод",
        }


class GuardDB:
    def __init__(self, stories):
        self.stories = stories

    def story_candidates(self, hours=24, limit=40):
        return self.stories

    def story_facts(self, event_id, limit=8):
        return []

    def story_messages(self, event_id, limit=4):
        return []


def test_new_guard_recovers_kasyanov_duplicate_after_initial_new():
    from newsbot.core.story_manager import StoryManager

    # The real earlier story.
    target = _story(
        317,
        "Скончался актер и основатель карате в СССР Тадеуш Касьянов",
    )

    # Deliberate lexical distractors. They resemble the later headline
    # but refer to different people.
    distractors = [
        _story(
            i,
            f"Умер заслуженный тренер России по карате Сергей Иванов {i}",
        )
        for i in range(250, 257)
    ]

    settings = SimpleNamespace(
        story_hours=24,
        story_candidate_limit=40,
        story_semantic_candidates=2,
        story_identity_candidates=5,
        story_new_guard_candidates=5,
    )

    db = GuardDB(distractors + [target])
    llm = GuardLLM()
    manager = StoryManager(settings, db, llm)

    result = asyncio.run(
        manager.classify(
            "Умер заслуженный тренер России по карате Тадеуш Касьянов",
            "Умер заслуженный тренер России по карате Тадеуш Касьянов.",
            "society",
            raw_title="Умер заслуженный тренер России по карате Тадеуш Касьянов",
            raw_text="Умер заслуженный тренер России по карате Тадеуш Касьянов.",
        )
    )

    assert result["relation"] == "duplicate"
    assert result["story_id"] == 317

    # First call = intentionally wrong initial NEW.
    # At least one subsequent guard call must have inspected Story 317.
    assert len(llm.calls) >= 2
    assert any("STORY_ID: 317" in c for c in llm.calls[1:])


class DifferentEventLLM:
    def __init__(self):
        self.calls = []

    async def classify_story(self, context):
        self.calls.append(context)
        return {
            "relation": "new",
            "story_id": 0,
            "confidence": 0.99,
            "new_facts": ["Новый эпизод"],
            "update_title": "",
            "update_text": "",
            "reason": "Другой конкретный эпизод",
        }


def _run_story_classification(llm, target, incoming_title, incoming_text):
    from newsbot.core.story_manager import StoryManager

    settings = SimpleNamespace(
        story_hours=24,
        story_candidate_limit=40,
        story_semantic_candidates=2,
        story_identity_candidates=5,
        story_new_guard_candidates=5,
    )

    db = GuardDB([target])
    manager = StoryManager(settings, db, llm)

    return asyncio.run(
        manager.classify(
            incoming_title,
            incoming_text,
            "society",
            raw_title=incoming_title,
            raw_text=incoming_text,
        )
    )


def test_same_person_different_event_remains_new():
    target = _story(
        317,
        "Скончался актер и основатель карате в СССР Тадеуш Касьянов",
    )

    llm = DifferentEventLLM()

    result = _run_story_classification(
        llm,
        target,
        "Тадеуш Касьянов представил новую программу Федерации карате России",
        "Тадеуш Касьянов представил новую программу Федерации карате России.",
    )

    assert result["relation"] == "new"
    assert result["story_id"] == 0
    assert len(llm.calls) >= 2


def test_same_person_same_event_type_but_different_episode_remains_new():
    target = _story(
        317,
        "Скончался актер и основатель карате в СССР Тадеуш Касьянов",
    )

    llm = DifferentEventLLM()

    result = _run_story_classification(
        llm,
        target,
        "Тадеуш Касьянов получил новую награду за развитие карате",
        "Тадеуш Касьянов получил новую награду за развитие карате.",
    )

    assert result["relation"] == "new"
    assert result["story_id"] == 0
    assert len(llm.calls) >= 2


def test_new_guard_includes_identity_story_outside_top_limit():
    from newsbot.core.story_manager import StoryManager

    target = _story(
        317,
        "Скончался актер и основатель карате в СССР Тадеуш Касьянов",
    )

    distractors = [
        _story(
            i,
            f"Умер заслуженный тренер России по карате Сергей Иванов {i}",
        )
        for i in range(250, 257)
    ]

    settings = SimpleNamespace(
        story_hours=24,
        story_candidate_limit=40,
        story_semantic_candidates=2,
        story_identity_candidates=5,
        story_new_guard_candidates=3,
    )

    db = RetrievalDB(distractors + [target])
    manager = StoryManager(settings, db, StoryLLM({}))

    title = "Умер заслуженный тренер России по карате Тадеуш Касьянов"
    text = "Умер заслуженный тренер России по карате Тадеуш Касьянов."

    guard_candidates = manager._guard_candidate_rows(
        title,
        text,
    )

    guard_ids = {
        candidate_id
        for _, _, candidate_id, _ in guard_candidates
    }

    assert 317 in guard_ids
