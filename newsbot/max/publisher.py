import logging
from pathlib import Path
from pymax import Client,Photo
log=logging.getLogger('max')
class MaxPublisher:
    def __init__(self,s):
        self.s=s;self.session_dir=Path(s.max_work_dir);self.session_dir.mkdir(parents=True,exist_ok=True);self.client=Client(phone=s.max_phone,session_name=s.max_session,work_dir=str(self.session_dir));self.channel_id=self._id(s.max_channel_id)
    @staticmethod
    def _id(v):
        try:return int(str(v).strip()) if v else None
        except (TypeError,ValueError):return None
    def connected(self):
        v=getattr(self.client,'is_connected',False);return bool(v() if callable(v) else v)
    async def start(self):
        if not self.connected(): await self.client.connect();log.info('MAX connected')
    async def channel(self):
        if self.channel_id:return self.channel_id
        await self.start()
        for chat in await self.client.fetch_chats():
            hay=' '.join(str(getattr(chat,k,'') or '') for k in ('title','name','username','link')).lower()
            if 'channel_al' in hay:self.channel_id=self._id(getattr(chat,'id',None));return self.channel_id
        raise RuntimeError('MAX_CHANNEL_ID not found')
    async def publish(self,title,text,media_path='',reply_to=None):
        await self.start();cid=await self.channel();body=f'{title.strip()}\n\n{text.strip()}'.strip();kw={'chat_id':cid,'text':body}
        if reply_to is not None:kw['reply_to']=int(reply_to)
        if media_path and Path(media_path).is_file():kw['attachments']=[Photo(path=str(media_path))]
        m=await self.client.send_message(**kw);mid=getattr(m,'id',m);log.info('MAX publish id=%s reply_to=%s',mid,reply_to);return mid
    async def close(self):
        try:await self.client.close()
        except Exception:pass
