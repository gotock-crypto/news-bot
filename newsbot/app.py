import asyncio
from .config import load_settings,load_sources
from .logging_setup import setup
from .db import DB
from .llm.adapter import LLM
from .max.publisher import MaxPublisher
from .core.pipeline import Pipeline
from .telegram.collector import TelegramCollector
from .telegram.publisher import TelegramPublisher
from .admin_bot import AdminBot
class App:
    def __init__(self):
        self.s=load_settings();setup(self.s.log_level);self.db=DB(self.s.db_path);self.db.seed_sources(load_sources(self.s.source_path));self.db.cleanup(self.s.retention_days);self.llm=LLM(self.s);self.max=MaxPublisher(self.s);self.telegram=TelegramPublisher(self.s,self.db);self.pipeline=Pipeline(self.s,self.db,self.llm,self.max,self.telegram);self.collector=TelegramCollector(self.s,self.db,self.pipeline.handle);self.admin=AdminBot(self.s,self.db)
    async def run(self):
        tasks=[asyncio.create_task(self.collector.start())]
        if self.s.admin_bot_token and self.s.admin_ids:tasks.append(asyncio.create_task(asyncio.to_thread(self.admin.run_forever)))
        try:await asyncio.gather(*tasks)
        finally:await self.collector.stop();await self.max.close();await self.telegram.close()
