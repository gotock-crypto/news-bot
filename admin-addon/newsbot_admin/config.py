import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# The addon lives inside /opt/news-bot-v1/admin-addon, while the production
# project root is /opt/news-bot-v1. Resolve paths against that project root so
# the addon can share the existing news.db and sources.json without modifying
# the primary service.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADDON_ROOT = Path(__file__).resolve().parents[1]

# Load primary settings first, then admin overrides.
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env.admin", override=True)

def env(name, default=""):
    return os.getenv(name, default).strip()

def env_int(name, default=0):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default

@dataclass(frozen=True)
class AdminSettings:
    db_file: str
    source_config: str
    bot_token: str
    admin_ids: tuple[int, ...]
    poll_seconds: int
    tg_session: str
    max_session: str
    max_work_dir: str
    max_phone: str
    max_channel_id: str
    auto_publish: bool
    hot_backfill_limit: int
    hot_poll_seconds: int
    log_level: str
    @property
    def db_path(self):
        return PROJECT_ROOT / self.db_file
    @property
    def source_path(self):
        return PROJECT_ROOT / self.source_config
    @property
    def tg_session_path(self):
        return str(PROJECT_ROOT / self.tg_session)
    @property
    def max_dir(self):
        return PROJECT_ROOT / self.max_work_dir


def load_settings():
    raw = [x.strip() for x in env("ADMIN_IDS").split(",") if x.strip()]
    ids = []
    for x in raw:
        try:
            ids.append(int(x))
        except ValueError:
            pass
    return AdminSettings(
        db_file=env("DB_FILE", "runtime/news.db"),
        source_config=env("SOURCE_CONFIG", "sources.json"),
        bot_token=env("ADMIN_BOT_TOKEN"),
        admin_ids=tuple(ids),
        poll_seconds=env_int("ADMIN_BOT_POLL_SECONDS", 2),
        tg_session=env("ADMIN_TG_SESSION", "runtime/telegram/newsbot_admin"),
        max_session=env("ADMIN_MAX_SESSION", "runtime/max-admin/max-admin.db"),
        max_work_dir=env("ADMIN_MAX_WORK_DIR", "runtime/max-admin"),
        max_phone=env("ADMIN_MAX_PHONE", env("MAX_PHONE")),
        max_channel_id=env("MAX_CHANNEL_ID"),
        auto_publish=env("AUTO_PUBLISH", "1") == "1",
        hot_backfill_limit=env_int("ADMIN_HOT_BACKFILL_LIMIT", 5),
        hot_poll_seconds=env_int("ADMIN_HOT_POLL_SECONDS", 5),
        log_level=env("LOG_LEVEL", "INFO"),
    )
