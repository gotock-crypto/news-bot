import asyncio
from .config import load_settings,load_sources
from .logging_setup import setup
from .db import DB
from .llm.adapter import LLM
from .max.publisher import MaxPublisher
from .core.pipeline import Pipeline
from .telegram.collector import TelegramCollector
from .telegram.publisher import TelegramPublisher
from .threads.publisher import ThreadsPublisher
from .admin_bot import AdminBot
import logging

log = logging.getLogger(__name__)
class App:
    def __init__(self):
        self.s=load_settings();setup(self.s.log_level);self.db=DB(self.s.db_path);self.db.seed_sources(load_sources(self.s.source_path));self.db.cleanup(self.s.retention_days);self.llm=LLM(self.s);self.max=MaxPublisher(self.s);self.telegram=TelegramPublisher(self.s,self.db);self.threads=ThreadsPublisher(self.s,self.db);self.pipeline=Pipeline(self.s,self.db,self.llm,self.max,self.telegram,self.threads);self.collector=TelegramCollector(self.s,self.db,self.pipeline.handle);self.admin=AdminBot(self.s,self.db)
    async def run(self):
        admin_thread = None

        if self.s.admin_bot_token and self.s.admin_ids:
            import threading

            admin_thread = threading.Thread(
                target=self.admin.run_forever,
                name="admin-bot",
                daemon=True,
            )
            admin_thread.start()

        try:
            await self.collector.start()
        finally:
            if self.s.admin_bot_token and self.s.admin_ids:
                self.admin.stop()

            await self.collector.stop()
            await self.max.close()
            await self.telegram.close()
            await self.threads.close()

            if admin_thread and admin_thread.is_alive():
                timeout = max(2, int(getattr(self.s, "admin_poll_seconds", 5)) + 10)
                admin_thread.join(timeout=timeout)

                if admin_thread.is_alive():
                    log.warning(
                        "Admin Bot thread did not stop within %ss",
                        timeout,
                    )
