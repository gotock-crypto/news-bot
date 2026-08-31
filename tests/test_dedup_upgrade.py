from types import SimpleNamespace

from newsbot.db import DB
from newsbot.core.event_resolver import EventResolver


def settings():
    return SimpleNamespace(
        dedup_hours=24,
        event_resolver_hours=24,
        event_resolver_scan_limit=200,
        event_resolver_max_candidates=40,
        event_resolver_similarity_floor=0.10,
        event_resolver_confidence=0.78,
        event_resolver_fallback_similarity=0.94,
    )


def test_resolver_returns_one_latest_article_per_event(tmp_path):
    db = DB(tmp_path / "news.db")
    m1 = db.insert_message("a", 1, db.now(), "Пожар на складе в Москве")
    e1 = db.create_event("Пожар на складе в Москве", "russia", 5, .9, m1)
    a1 = db.add_article(e1, m1, "NEW", "Пожар на складе в Москве", "Пожар", .9)

    m2 = db.insert_message("b", 1, db.now(), "В Москве пожар на складе")
    db.attach_event(e1, m2)
    db.add_article(e1, m2, "UPGRADE", "Уточнение пожара", "Еще факт", .9)

    m3 = db.insert_message("c", 1, db.now(), "Авария в Казани")
    e2 = db.create_event("Авария в Казани", "russia", 5, .9, m3)
    db.add_article(e2, m3, "NEW", "Авария в Казани", "Авария", .9)

    rows = EventResolver(settings(), db)._candidate_rows()
    ids = [int(r["event_id"]) for r in rows]
    assert set(ids) == {e1, e2}
    assert len(ids) == 2


def test_resolver_ranks_same_event_high(tmp_path):
    db = DB(tmp_path / "news.db")
    m = db.insert_message("a", 1, db.now(), "Пожар на складе в Москве")
    e = db.create_event("Пожар на складе в Москве", "russia", 5, .9, m)
    db.add_article(e, m, "NEW", "Пожар на складе в Москве", "Пожар на складе", .9)
    rows = EventResolver(settings(), db)._candidate_rows()
    ranked = EventResolver(settings(), db)._rank(
        "В Москве произошел пожар на складе",
        "Пожар на складе в Москве",
        rows,
    )
    assert ranked
    assert int(ranked[0][1]["event_id"]) == e
    assert ranked[0][0] >= .72
