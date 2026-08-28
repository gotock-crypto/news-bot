import logging
import threading
from .config import load_settings
from .control_db import ControlDB
from .bot import TelegramAdminBot
from .hot_worker import HotWorker

def main():
    logging.basicConfig(level=getattr(logging,load_settings().log_level.upper(),logging.INFO),format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    s=load_settings(); db=ControlDB(s.db_path,s.source_path)
    bot=TelegramAdminBot(s,db)
    # Run Bot API admin UI in its own thread; Telethon hot worker stays async.
    t=threading.Thread(target=bot.run,name='admin-bot',daemon=True); t.start()
    import asyncio
    asyncio.run(HotWorker(s,db).start())

if __name__=='__main__': main()
