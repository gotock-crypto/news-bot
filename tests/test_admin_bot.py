from types import SimpleNamespace

from newsbot.admin_bot import AdminBot


class FakeDB:
    def dashboard_stats(self, hours=24):
        assert hours == 24
        return {
            "jobs": {
                "pending": 2,
                "processing": 1,
                "done": 10,
                "failed": 0,
            },
            "telegram": {
                "destinations": 3,
                "enabled": 2,
                "sent": 5,
                "failed": 1,
                "pending": 1,
            },
            "threads": {
                "total": 4,
                "sent": 3,
                "failed": 1,
                "last_updated": "2026-09-08T09:00:00+00:00",
            },
            "poll": {
                "last_poll": "2026-09-08T09:00:00+00:00",
                "poll_errors": "0",
            },
        }

    def get_source(self, username):
        return {
            "username": username,
            "enabled": 1,
            "owner": "addon",
            "priority": 5,
            "category": "auto",
            "reliability": 0.7,
            "last_message_id": 123,
        }

    def set_source(self, username, enabled):
        self.last_set_source = (username, enabled)

    def list_sources(self):
        return []

    def list_telegram_destinations(self):
        return []


def make_bot():
    settings = SimpleNamespace(
        admin_bot_token="test-token",
        admin_ids={1},
    )
    return AdminBot(settings, FakeDB())


def callback(data):
    return {
        "id": "callback-id",
        "from": {"id": 1},
        "message": {"chat": {"id": 100}},
        "data": data,
    }


def test_menu_contains_expected_callbacks():
    bot = make_bot()

    callbacks = {
        button["callback_data"]
        for row in bot.menu()
        for button in row
    }

    assert callbacks == {
        "stats",
        "status",
        "dashboard",
        "sources",
        "srcstats",
        "telegram",
        "stories",
        "add",
    }


def test_dashboard_renders_stats():
    bot = make_bot()
    sent = []

    bot.send = lambda chat_id, text, keyboard=None: sent.append(
        (chat_id, text, keyboard)
    )

    bot.dashboard(100)

    assert len(sent) == 1
    chat_id, text, keyboard = sent[0]

    assert chat_id == 100
    assert "NEWS BOT DASHBOARD" in text
    assert "Pending: 2" in text
    assert "Processing: 1" in text
    assert "TELEGRAM 24h" in text
    assert "THREADS 24h" in text
    assert "POLLING" in text
    assert keyboard[0][0]["callback_data"] == "dashboard"


def test_callback_routes_dashboard():
    bot = make_bot()
    called = []

    bot.api = lambda *args, **kwargs: None
    bot.dashboard = lambda chat_id: called.append(chat_id)

    bot.callback(callback("dashboard"))

    assert called == [100]


def test_callback_add_sets_state():
    bot = make_bot()
    sent = []

    bot.api = lambda *args, **kwargs: None
    bot.send = lambda chat_id, text, keyboard=None: sent.append(
        (chat_id, text)
    )

    bot.callback(callback("add"))

    assert bot.state[1] == "add"
    assert sent
    assert "Пришлите username" in sent[0][1]


def test_callback_toggle_source():
    bot = make_bot()

    bot.api = lambda *args, **kwargs: None
    bot.send = lambda *args, **kwargs: None

    bot.callback(callback("toggle:test_channel"))

    assert bot.db.last_set_source == ("test_channel", False)
