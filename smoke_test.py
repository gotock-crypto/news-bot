import asyncio
from newsbot.config import load_settings
from newsbot.max.publisher import MaxPublisher
async def main():
    s=load_settings(); p=MaxPublisher(s)
    try:
        await p.start(); mid=await p.publish("🧪 News Bot", "Тестовая публикация MAX."); print(f"SUCCESS: message_id={mid}")
    finally: await p.close()
if __name__=="__main__": asyncio.run(main())
