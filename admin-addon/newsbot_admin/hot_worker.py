import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events

# Reuse the existing News Bot modules without editing them.
from newsbot.config import load_settings as load_news_settings
from newsbot.db import DB
from newsbot.core.dedup import find_similar
from newsbot.llm.adapter import LLM
from newsbot.max.publisher import MaxPublisher
from .config import load_settings
from .control_db import ControlDB

log=logging.getLogger('hot_worker')

class HotWorker:
    def __init__(self,s,control):
        self.s=s; self.control=control; self.ns=load_news_settings(); self.db=DB(self.ns.db_path); self.llm=LLM(self.ns)
        # Separate session files prevent locking the live collector's Telethon/MAX sessions.
        self.tg=TelegramClient(s.tg_session_path,self.ns.tg_api_id,self.ns.tg_api_hash)
        self.max=MaxPublisher(type('S',(),dict(
            max_work_dir=str(s.max_dir), max_session=s.max_session, max_phone=s.max_phone, max_channel_id=s.max_channel_id
        ))())
        self.entities={}; self.sources={}; self.watermarks={}; self.handler=None

    async def start(self):
        if not self.ns.tg_api_id or not self.ns.tg_api_hash: raise RuntimeError('TG_API_ID/TG_API_HASH are required')
        await self.tg.start(phone=self.ns.tg_phone or None)
        self.handler=self.on_new
        await self.sync_sources(force=True)
        self.tg.add_event_handler(self.handler,events.NewMessage(chats=list(self.entities.values()))) if self.entities else None
        asyncio.create_task(self.watch_sources())
        asyncio.create_task(self.poll())
        log.info('Hot-source worker online sources=%s',list(self.sources))
        await self.tg.run_until_disconnected()

    async def resolve(self,u):
        entity=await self.tg.get_entity(u); return entity,getattr(entity,'id',None)

    async def sync_sources(self,force=False):
        rows=self.control.hot_sources(); wanted={r['username']:r for r in rows}
        add=[u for u in wanted if u not in self.sources]
        remove=[u for u in self.sources if u not in wanted]
        for u in remove:
            eid=self.sources[u]['entity_id']; self.entities.pop(eid,None); self.sources.pop(u,None); self.watermarks.pop(u,None)
        if self.handler is not None and (add or remove):
            try:self.tg.remove_event_handler(self.handler)
            except Exception:pass
        for u in add:
            try:
                ent,eid=await self.resolve(u);
                if eid is None: continue
                self.sources[u]={'row':wanted[u],'entity_id':eid}; self.entities[eid]=ent
                latest=await self.tg.get_messages(ent,limit=1); self.watermarks[u]=latest[0].id if latest else 0
                row=wanted[u]
                # Existing primary sources are already handled by the unchanged
                # production collector. We monitor them here for unified hot
                # enable/disable control, but do not replay history on startup.
                # Newly-added addon sources get a small backfill.
                self.control.upsert_source(u,int(row['priority']),row['category'],float(row['reliability']),row['owner'],eid,getattr(ent,'title',None))
                log.info('Hot source resolved username=%s id=%s owner=%s',u,eid,row['owner'])
                if row['owner']=='addon':
                    await self.backfill(u,ent,self.watermarks[u])
            except Exception: log.exception('Hot source resolve failed username=%s',u)
        if self.handler is not None and self.entities and (add or remove):
            self.tg.add_event_handler(self.handler,events.NewMessage(chats=list(self.entities.values())))

    async def watch_sources(self):
        while True:
            await asyncio.sleep(5); await self.sync_sources()

    async def poll(self):
        while True:
            try:
                for u,meta in list(self.sources.items()):
                    latest=await self.tg.get_messages(meta['entity_id'],limit=3)
                    for m in reversed(list(latest or [])):
                        if not m.id or m.id<=self.watermarks.get(u,0): continue
                        self.watermarks[u]=m.id; await self.process(m,meta['row'])
            except asyncio.CancelledError: raise
            except Exception: log.exception('hot poll failed')
            await asyncio.sleep(self.s.hot_poll_seconds)

    async def on_new(self,event):
        cid=getattr(event,'chat_id',None)
        for u,meta in self.sources.items():
            if meta['entity_id']==cid:
                self.watermarks[u]=max(self.watermarks.get(u,0),event.message.id); await self.process(event.message,meta['row']); return

    async def backfill(self,u,ent,watermark):
        async for m in self.tg.iter_messages(ent,limit=self.s.hot_backfill_limit):
            if getattr(m,'id',0)>=watermark: continue
            await self.process(m,self.sources[u]['row'],backfill=True)

    async def process(self,message,source,backfill=False):
        text=(message.message or '').strip()
        if not text:return
        username=source['username']; msg_id=self.db.insert_message(username,message.id,message.date.isoformat() if message.date else '',text,f'https://t.me/{username}/{message.id}','',json.dumps({'source':username,'id':message.id,'hot_worker':True,'backfill':backfill},ensure_ascii=False),int(source['priority']),float(source['reliability']),source['category'])
        if not msg_id:return
        recent=self.db.recent_messages(self.ns.dedup_hours); others=[r for r in recent if r['id']!=msg_id]; sim=find_similar(text,others,self.ns.similarity_threshold)
        if sim:
            existing=self.db.find_event_for_message(sim[1]['id'])
            if existing:
                eid=existing['event_id']; self.db.attach_event(eid,msg_id); await self.process_event(eid,not backfill); return
        data=await self.llm.edit([{'source':username,'url':f'https://t.me/{username}/{message.id}','text':text,'reliability':float(source['reliability']),'priority':int(source['priority'])}])
        if not data.get('publish'):return
        eid=self.db.create_event(data.get('title',''),data.get('category','other'),int(data.get('priority',0)),float(data.get('confidence',0)),msg_id)
        self.db.set_article(eid,data.get('title',''),data.get('text',''),data.get('category','other'),float(data.get('confidence',0)),data.get('reason',''))
        if not backfill: await self.maybe_publish(eid,data)

    async def process_event(self,eid,allow_publish=True):
        rows=self.db.event_messages(eid); data=await self.llm.edit([{'source':r['source'],'url':r['url'],'text':r['text'],'reliability':r['source_reliability'],'priority':r['source_priority']} for r in rows])
        if not data.get('publish'): self.db.mark_rejected(eid,data.get('reason','')); return
        self.db.update_event(eid,data.get('title',''),data.get('category','other'),int(data.get('priority',0)),float(data.get('confidence',0))); self.db.set_article(eid,data.get('title',''),data.get('text',''),data.get('category','other'),float(data.get('confidence',0)),data.get('reason',''))
        if allow_publish: await self.maybe_publish(eid,data)

    async def maybe_publish(self,eid,data):
        if not self.s.auto_publish:return
        if int(data.get('priority',0))<int(self.ns.min_priority_publish):return
        if float(data.get('confidence',0))<float(self.ns.min_confidence_publish):return
        with self.db.conn() as c:
            a=c.execute('SELECT * FROM articles WHERE event_id=?',(eid,)).fetchone()
        if not a:return
        mid=await self.max.publish(a['title'],a['text'],'')
        if mid is not None:self.db.mark_published(eid,mid)
