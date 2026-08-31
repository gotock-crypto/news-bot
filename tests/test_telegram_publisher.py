from types import SimpleNamespace

from newsbot.db import DB
from newsbot.telegram.publisher import TelegramPublisher


def settings():
    return SimpleNamespace(admin_bot_token="TEST")


def test_telegram_destination_and_delivery(tmp_path):
    db = DB(tmp_path / "news.db")
    destination = db.upsert_telegram_destination(
        -100123,
        "supergroup",
        "News Group",
        42,
        "Новости",
        777,
    )
    message = db.insert_message("source", 1, db.now(), "Новость")
    event = db.create_event("Новость", "world", 80, .9, message)
    article = db.add_article(event, message, "NEW", "Новость", "Текст", .9)

    delivery = db.record_telegram_delivery(
        destination,
        article,
        "NEW",
        "sent",
        telegram_message_id=55,
    )

    assert delivery == 1
    row = db.last_telegram_delivery(destination, event)
    assert row["telegram_message_id"] == 55
    assert row["status"] == "sent"


def test_publisher_skips_already_sent(monkeypatch, tmp_path):
    db = DB(tmp_path / "news.db")
    destination = db.upsert_telegram_destination(-100123, "supergroup", "News", 9, "Topic")
    message = db.insert_message("source", 1, db.now(), "Новость")
    event = db.create_event("Новость", "world", 80, .9, message)
    article = db.add_article(event, message, "NEW", "Новость", "Текст", .9)
    db.record_telegram_delivery(destination, article, "NEW", "sent", telegram_message_id=99)

    publisher = TelegramPublisher(settings(), db)
    called = []

    async def fake_send(*args, **kwargs):
        called.append(True)
        return {"message_id": 100}

    monkeypatch.setattr(publisher, "_send", fake_send)

    import asyncio
    asyncio.run(publisher.publish_article(event, article, "NEW", "Новость", "Текст"))
    assert called == []
