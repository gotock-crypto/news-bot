import json,time,logging,requests
log=logging.getLogger('admin')
class AdminBot:
    def __init__(self,s,db):self.s=s;self.db=db;self.offset=0;self.state={};self.http=requests.Session();self.base=f'https://api.telegram.org/bot{s.admin_bot_token}' if s.admin_bot_token else ''
    def api(self,m,p=None,timeout=40):
        r=self.http.post(f'{self.base}/{m}',json=p or {},timeout=timeout);r.raise_for_status();d=r.json();
        if not d.get('ok'):raise RuntimeError(str(d))
        return d.get('result')
    def auth(self,u):return u in self.s.admin_ids
    def send(self,c,t,k=None):
        p={'chat_id':c,'text':t};
        if k:p['reply_markup']=json.dumps({'inline_keyboard':k},ensure_ascii=False)
        self.api('sendMessage',p)
    def menu(self):return [[{'text':'📊 Статистика','callback_data':'stats'},{'text':'🟢 Статус','callback_data':'status'}],[{'text':'📰 Источники','callback_data':'sources'},{'text':'📈 Источники 24ч','callback_data':'srcstats'}],[{'text':'➕ Добавить','callback_data':'add'}]]
    def source_menu(self):
        rows=[]
        for r in self.db.list_sources():rows.append([{'text':('🟢 ' if r['enabled'] else '🔴 ')+'@'+r['username'],'callback_data':'src:'+r['username']}])
        rows.append([{'text':'➕ Добавить','callback_data':'add'},{'text':'⬅️ Меню','callback_data':'home'}]);return rows
    def handle(self,u):
        if 'callback_query' in u:return self.callback(u['callback_query'])
        m=u.get('message');
        if not m:return
        uid=m.get('from',{}).get('id');cid=m['chat']['id'];text=(m.get('text') or '').strip()
        if not self.auth(uid):return self.send(cid,'⛔ Доступ запрещён.') if text.startswith('/') else None
        if self.state.get(uid)=='add':return self.add(uid,cid,text)
        if text in ('/start','/menu'):return self.send(cid,'🛠 News Bot — управление',self.menu())
        if text.startswith('/stats'):return self.stats(cid)
        if text.startswith('/status'):return self.status(cid)
        if text.startswith('/sources'):return self.send(cid,'📰 Источники',self.source_menu())
        self.send(cid,'Выберите действие:',self.menu())
    def stats(self,c):
        a=self.db.stats(24);b=self.db.stats(168);self.send(c,f'📊 24 часа\nПолучено: {a["received"]}\nСобытий: {a["events"]}\nUpgrade: {a["upgrades"]}\nОпубликовано: {a["published"]}\n\n📊 7 дней\nПолучено: {b["received"]}\nСобытий: {b["events"]}\nUpgrade: {b["upgrades"]}\nОпубликовано: {b["published"]}',self.menu())
    def status(self,c):self.send(c,f'🟢 NEWS BOT\n\nTelegram: {self.db.get_state("telegram_status","unknown")}\nИсточников: {len(self.db.list_sources(True))}\nПоследний poll: {self.db.get_state("last_poll","—")}\nОшибок polling: {self.db.get_state("poll_errors","0")}\nDB: 🟢\nAdmin Bot: 🟢',self.menu())
    def add(self,uid,c,text):
        u=text.split()[0].lstrip('@').lower() if text.split() else ''
        if not u or not u.replace('_','').isalnum():return self.send(c,'❌ Некорректный username.',self.menu())
        self.db.upsert_source(u);self.state.pop(uid,None);self.send(c,f'✅ @{u} добавлен. История не реплеится: первый poll установит watermark.',self.source_menu())
    def callback(self,q):
        uid=q.get('from',{}).get('id');c=q.get('message',{}).get('chat',{}).get('id');d=q.get('data','')
        if not self.auth(uid):return
        try:self.api('answerCallbackQuery',{'callback_query_id':q.get('id')})
        except Exception:pass
        if d in ('home','refresh'):return self.send(c,'🛠 News Bot — управление',self.menu())
        if d=='stats':return self.stats(c)
        if d=='status':return self.status(c)
        if d=='sources':return self.send(c,'📰 Источники',self.source_menu())
        if d=='srcstats':
            rows=self.db.source_stats(24);return self.send(c,'📈 Источники — 24ч\n\n'+('\n'.join(f'@{r["source"]} — {r["received"]}' for r in rows) or 'Нет сообщений.'),self.menu())
        if d=='add':self.state[uid]='add';return self.send(c,'➕ Пришлите username канала, например @example_news.')
        if d.startswith('src:'):
            u=d[4:];r=self.db.get_source(u)
            if not r:return
            k=[[{'text':'⏸ Выключить' if r['enabled'] else '▶️ Включить','callback_data':'toggle:'+u}]]
            if r['owner']=='addon':k[0].append({'text':'🗑 Удалить','callback_data':'delete:'+u})
            k.append([{'text':'⬅️ Источники','callback_data':'sources'}]);return self.send(c,f'@{u}\n\nСтатус: {"🟢" if r["enabled"] else "🔴"}\nOwner: {r["owner"]}\nPriority: {r["priority"]}\nCategory: {r["category"]}\nReliability: {r["reliability"]:.2f}\nWatermark: {r["last_message_id"]}',k)
        if d.startswith('toggle:'):
            u=d[7:];r=self.db.get_source(u);self.db.set_source(u,not bool(r['enabled']));return self.send(c,'Готово.',self.source_menu())
        if d.startswith('delete:'):self.db.delete_source(d[7:]);return self.send(c,'Готово.',self.source_menu())
    def run_forever(self):
        if not self.s.admin_bot_token or not self.s.admin_ids:log.warning('Admin Bot disabled');return
        self.api('getMe');log.info('Admin Bot started')
        while True:
            try:
                for u in self.api('getUpdates',{'offset':self.offset,'timeout':self.s.admin_poll_seconds,'allowed_updates':['message','callback_query']},timeout=self.s.admin_poll_seconds+10) or []:self.offset=u['update_id']+1;self.handle(u)
            except Exception:log.exception('admin polling failed');time.sleep(3)
