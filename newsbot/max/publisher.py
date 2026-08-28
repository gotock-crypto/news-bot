import logging
from pathlib import Path
from pymax import Client, Photo

log = logging.getLogger("max")

class MaxPublisher:
    def __init__(self, s):
        self.s=s
        self.session_dir=Path(s.max_work_dir); self.session_dir.mkdir(parents=True,exist_ok=True)
        self.session_file=self.session_dir/s.max_session
        self.client=Client(phone=s.max_phone, session_name=s.max_session, work_dir=str(self.session_dir))
        self.channel_id=self._parse_id(s.max_channel_id)
    @staticmethod
    def _parse_id(value):
        try:
            value=str(value or '').strip(); return int(value) if value else None
        except (TypeError,ValueError): return None
    def is_connected(self):
        value=getattr(self.client,'is_connected',False); return bool(value() if callable(value) else value)
    async def start(self):
        if self.is_connected(): return
        if not self.session_file.exists() and not self.s.max_phone:
            raise RuntimeError(f'MAX session not found: {self.session_file}; MAX_PHONE is empty')
        log.info('MAX: starting client session=%s',self.session_file)
        await self.client.connect()
        log.info('MAX: connected profile=%s',getattr(self.client,'me',None))
    async def resolve_channel(self):
        if self.channel_id: return self.channel_id
        if not self.is_connected(): await self.start()
        chats=await self.client.fetch_chats(); log.info('MAX: fetched %s chats',len(chats))
        for chat in chats:
            hay=' '.join(str(getattr(chat,k,'') or '') for k in ('title','name','username','link')).lower()
            if 'channel_al' in hay:
                cid=self._parse_id(getattr(chat,'id',None))
                if cid is not None: self.channel_id=cid; return cid
        raise RuntimeError('MAX channel_al not found; set MAX_CHANNEL_ID in .env')
    async def publish(self,title,text,media_path=''):
        if not self.is_connected(): await self.start()
        if not self.channel_id: await self.resolve_channel()
        title=str(title or '').strip(); text=str(text or '').strip(); body=f'{title}\n\n{text}'.strip() if title and text else (title or text)
        if not body: raise ValueError('Cannot publish empty MAX message')
        kwargs={'chat_id':self.channel_id,'text':body}
        if media_path:
            path=Path(media_path)
            if path.is_file(): kwargs['attachments']=[Photo(path=str(path))]
            else: log.warning('MAX: media not found: %s',path)
        message=await self.client.send_message(**kwargs); mid=getattr(message,'id',message)
        log.info('MAX: published chat=%s message=%s',self.channel_id,mid); return mid
    async def close(self):
        try: await self.client.close()
        except Exception: log.debug('MAX: close error',exc_info=True)
