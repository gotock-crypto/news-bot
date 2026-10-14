import asyncio
from types import SimpleNamespace

from newsbot.db import DB
from newsbot.core.pipeline import Pipeline


class FakeLLM:
    def __init__(self, editorial, update=None, compose=None):
        self.editorial = editorial
        self.update = update or {"status": "ok", "important": False, "score": 0, "reason": "repeat"}
        self.compose = compose or {"status": "ok", "publish": False, "confidence": 0, "title": "", "text": "", "reason": "no"}

    async def edit(self, sources): return self.editorial
    async def classify_update(self, *args): return self.update
    async def compose_event_update(self, *args): return self.compose


class FakeMax:
    def __init__(self): self.calls = []
    async def publish(self, title, text, *args, **kwargs):
        self.calls.append((title, text, kwargs)); return "max-1"


def settings(**kw):
    base = dict(auto_publish=True, min_priority_publish=0, min_confidence_publish=.82,
                dedup_hours=24, event_resolver_hours=24, event_resolver_scan_limit=200,
                event_resolver_max_candidates=40, event_resolver_similarity_floor=.10,
                event_resolver_confidence=.78, event_resolver_min_interval_seconds=.01,
                llm_min_interval_seconds=.01, groq_cooldown_seconds=1, groq_key="",
                gigachat_key="", mistral_key="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_existing_event_score_below_80_is_not_published(tmp_path):
    db = DB(tmp_path / "db.sqlite")
    m = db.insert_message("src", 1, db.now(), "original")
    eid = db.create_event("Original", "other", 5, .9, m)
    aid = db.add_article(eid, m, "NEW", "Original", "original", .9)
    db.mark_published(eid, aid, "parent")
    m2 = db.insert_message("src", 2, db.now(), "minor detail")
    max_client = FakeMax()
    pipe = Pipeline(settings(), db, FakeLLM({}, {"status":"ok","important":False,"score":60,"reason":"minor"}), max_client)
    asyncio.run(pipe.existing_event(eid, db.conn().__enter__() if False else db.conn)) if False else None
    with db.conn() as c: row = c.execute("SELECT * FROM messages WHERE id=?", (m2,)).fetchone()
    asyncio.run(pipe.existing_event(eid, row))
    assert max_client.calls == []


def test_existing_event_score_80_publishes(tmp_path):
    db = DB(tmp_path / "db.sqlite")
    m = db.insert_message("src", 1, db.now(), "original")
    eid = db.create_event("Original", "other", 5, .9, m)
    aid = db.add_article(eid, m, "NEW", "Original", "original", .9)
    db.mark_published(eid, aid, "parent")
    m2 = db.insert_message("src", 2, db.now(), "new major episode")
    with db.conn() as c: row = c.execute("SELECT * FROM messages WHERE id=?", (m2,)).fetchone()
    max_client = FakeMax()
    compose = {"status":"ok","publish":True,"confidence":.95,"title":"Major update","text":"New major episode","reason":"new episode"}
    pipe = Pipeline(settings(), db, FakeLLM({}, {"status":"ok","important":True,"score":80,"reason":"major"}, compose), max_client)
    asyncio.run(pipe.existing_event(eid, row))
    assert len(max_client.calls) == 1
    assert db.last_publication(eid)["kind"] == "UPGRADE"


def test_classifier_unavailable_does_not_become_duplicate(tmp_path):
    db = DB(tmp_path / "db.sqlite")
    m = db.insert_message("src", 1, db.now(), "original")
    eid = db.create_event("Original", "other", 5, .9, m)
    aid = db.add_article(eid, m, "NEW", "Original", "original", .9)
    db.mark_published(eid, aid, "parent")
    m2 = db.insert_message("src", 2, db.now(), "maybe update")
    with db.conn() as c: row = c.execute("SELECT * FROM messages WHERE id=?", (m2,)).fetchone()
    pipe = Pipeline(settings(), db, FakeLLM({}, {"status":"unavailable","score":0}), FakeMax())
    try:
        asyncio.run(pipe.existing_event(eid, row))
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("unavailable classifier must be retryable")
