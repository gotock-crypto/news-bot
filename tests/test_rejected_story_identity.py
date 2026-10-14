import asyncio
from types import SimpleNamespace

from newsbot.db import DB
from newsbot.core.pipeline import Pipeline


class FakeLLM:
    async def edit(self, sources):
        return {
            "publish": False,
            "title": "",
            "text": "",
            "category": "other",
            "priority": 0,
            "confidence": 0,
            "reason": "материал недостаточно подробный",
        }

    async def classify_story(self, context):
        return {
            "relation": "duplicate",
            "story_id": 1,
            "confidence": 0.98,
            "new_facts": [],
            "update_title": "",
            "update_text": "",
            "reason": "тот же конкретный эпизод",
        }


class FakeMax:
    def __init__(self):
        self.calls = []

    async def publish(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "max-1"


def settings():
    return SimpleNamespace(
        auto_publish=True,
        min_priority_publish=0,
        min_confidence_publish=.8,
        dedup_hours=24,
        story_hours=24,
        story_candidate_limit=60,
        story_semantic_candidates=8,
    )


def test_rejected_message_attaches_to_existing_story(tmp_path):
    db = DB(tmp_path / "db.sqlite")

    m1 = db.insert_message(
        "src1", 1, db.now(),
        "Офицера ВКС РФ госпитализировали после покушения в Энгельсе"
    )
    eid = db.create_event(
        "Покушение на военнослужащего в Энгельсе",
        "other", 5, .95, m1
    )

    m2 = db.insert_message(
        "src2", 2, db.now(),
        "Возбуждено уголовное дело о покушении на военнослужащего "
        "в Саратовской области"
    )

    max_client = FakeMax()
    pipe = Pipeline(settings(), db, FakeLLM(), max_client)

    asyncio.run(pipe.handle(m2, "src2"))

    with db.conn() as c:
        events = c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        relation = c.execute(
            "SELECT event_id FROM event_messages WHERE message_id=?",
            (m2,),
        ).fetchone()

    assert events == 1
    assert relation is not None
    assert relation["event_id"] == eid
    assert max_client.calls == []
