import json
import logging
import time
import requests

log = logging.getLogger("admin_bot")

class TelegramAdminBot:
    def __init__(self, s, db):
        self.s=s; self.db=db; self.offset=0
        self.base=f"https://api.telegram.org/bot{s.bot_token}"
        self.session=requests.Session(); self.state={}

    def api(self, method, payload=None, timeout=40):
        r=self.session.post(f"{self.base}/{method}", json=payload or {}, timeout=timeout)
        r.raise_for_status(); data=r.json()
        if not data.get("ok"): raise RuntimeError(str(data))
        return data.get("result")

    def send(self, chat_id, text, keyboard=None):
        p={"chat_id":chat_id,"text":text}
        if keyboard: p["reply_markup"]=json.dumps({"inline_keyboard":keyboard},ensure_ascii=False)
        return self.api("sendMessage",p)

    def answer(self, cb_id, text=""):
        try:self.api("answerCallbackQuery",{"callback_query_id":cb_id,"text":text})
        except Exception: pass

    def authorized(self, uid): return uid in self.s.admin_ids

    def main_keyboard(self):
        return [[{"text":"📊 Статистика","callback_data":"stats"},{"text":"📈 По источникам","callback_data":"srcstats"}],
                [{"text":"🟢 Статус","callback_data":"status"},{"text":"📰 Источники","callback_data":"sources"}],
                [{"text":"➕ Добавить","callback_data":"add"},{"text":"🔄 Обновить","callback_data":"refresh"}]]

    def sources_keyboard(self):
        rows=[]
        for r in self.db.list_sources():
            icon="🟢" if r["enabled"] else "🔴"
            rows.append([{"text":f"{icon} @{r['username']}","callback_data":f"src:{r['username']}"}])
        rows.append([{"text":"➕ Добавить источник","callback_data":"add"},{"text":"⬅️ Назад","callback_data":"home"}])
        return rows

    def source_actions(self,u):
        r=self.db.get_source(u)
        if not r:return [[{"text":"⬅️ Назад","callback_data":"sources"}]]
        toggle="⏸ Выключить" if r["enabled"] else "▶️ Включить"
        delete_text="🗑 Удалить" if r["owner"]=='addon' else "🛡 Сохранить историю"
        delete_data=f"del:{u}"
        return [[{"text":toggle,"callback_data":f"toggle:{u}"},{"text":delete_text,"callback_data":delete_data}],
                [{"text":"⬅️ Назад","callback_data":"sources"}]]

    def handle_message(self,m):
        chat=m.get("chat",{}); uid=m.get("from",{}).get("id"); cid=chat.get("id"); text=(m.get("text") or "").strip()
        if not self.authorized(uid):
            if text.startswith("/"): self.send(cid,"⛔ Доступ запрещён.")
            return
        if text.startswith("/start") or text=="/menu": self.send(cid,"🛠 News Bot — управление",self.main_keyboard()); return
        if self.state.get(uid)=="add": self.finish_add(uid,cid,text); return
        if text.startswith("/stats"): self.show_stats(cid); return
        if text.startswith("/source_stats") or text.startswith("/srcstats"): self.show_source_stats(cid); return
        if text.startswith("/sources"): self.show_sources(cid); return
        if text.startswith("/status"): self.show_status(cid); return
        self.send(cid,"Выберите действие:",self.main_keyboard())

    def finish_add(self,uid,cid,text):
        u=text.split()[0].lstrip("@").lower() if text.split() else ""
        if not u or not u.replace("_","").isalnum():
            self.send(cid,"❌ Некорректный username. Пример: @rian_ru"); return
        try:
            self.db.upsert_source(u,owner="addon")
        except ValueError as exc:
            self.send(cid,f"⚠️ @{u} уже находится в основном списке источников.\n\nИспользуйте раздел «Источники», чтобы включить/выключить его.",self.main_keyboard())
            self.state.pop(uid,None)
            return
        except Exception:
            log.exception("failed to add source username=%s",u)
            self.send(cid,"❌ Не удалось добавить источник. Подробность в логах сервиса.",self.main_keyboard())
            return
        self.state.pop(uid,None)
        self.send(cid,f"✅ Источник @{u} добавлен.\n\nHot-source worker подхватит его автоматически без остановки основного сервиса.",self.main_keyboard())

    def show_stats(self,cid):
        blocks=[]
        for label,h in [("Сегодня",24),("7 дней",168)]:
            x=self.db.counts(h)
            blocks.append("📊 %s\n📥 Получено: %s\n🧩 Событий: %s\n🚫 Отклонено: %s\n📤 Опубликовано: %s\n🔗 Сообщений в событиях: %s" % (label,x["received"],x["events"],x["rejected"],x["published"],x["linked_messages"]))
        self.send(cid,"\n\n".join(blocks),self.main_keyboard())

    def show_source_stats(self,cid):
        rows=self.db.source_stats(24)
        if not rows:
            self.send(cid,"📈 За последние 24 часа сообщений нет.",self.main_keyboard()); return
        lines=["📈 Источники — 24 часа",""]
        for r in rows:
            linked=r['linked'] or 0
            lines.append(f"@{r['source']} — 📥 {r['received']}  🔗 {linked}")
        self.send(cid,"\n".join(lines),self.main_keyboard())

    def show_sources(self,cid): self.send(cid,"📰 Источники",self.sources_keyboard())

    def show_status(self,cid):
        rows=self.db.list_sources(); enabled=sum(1 for r in rows if r["enabled"]); addon=sum(1 for r in rows if r["enabled"] and r["owner"]=="addon")
        text="🟢 CONTROL PLANE ONLINE\n\nИсточников: %s\nHot/addon sources: %s\nDB: 🟢\nAdmin bot: 🟢\n\nВсе включённые источники управляются через единый Registry.\nОсновной News Bot можно не перезапускать." % (enabled,addon)
        self.send(cid,text,self.main_keyboard())

    def handle_callback(self,c):
        uid=c.get("from",{}).get("id"); cid=c.get("message",{}).get("chat",{}).get("id"); data=c.get("data","")
        if not self.authorized(uid): self.answer(c.get("id"),"Доступ запрещён"); return
        self.answer(c.get("id"))
        if data in ("home","refresh"): self.send(cid,"🛠 News Bot — управление",self.main_keyboard()); return
        if data=="stats": self.show_stats(cid); return
        if data=="srcstats": self.show_source_stats(cid); return
        if data=="status": self.show_status(cid); return
        if data=="sources": self.show_sources(cid); return
        if data=="add": self.state[uid]="add"; self.send(cid,"➕ Пришлите username канала, например @example_news."); return
        if data.startswith("src:"):
            u=data[4:]; r=self.db.get_source(u)
            if not r:return
            st=self.db.source_stat(u,24)
            text="@%s\n\nСтатус: %s\nРежим: %s\nPriority: %s\nCategory: %s\nReliability: %.2f\n\nЗа 24 часа: 📥 %s\nСвязано с событиями: 🔗 %s\nПоследнее сообщение: %s" % (u,"🟢 включён" if r["enabled"] else "🔴 выключен",r["owner"],r["priority"],r["category"],r["reliability"],st['received'] or 0,st['linked'] or 0,st['last_received'] or '—')
            self.send(cid,text,self.source_actions(u)); return
        if data.startswith("toggle:"):
            u=data[7:]; r=self.db.get_source(u)
            if r and r["enabled"]: self.db.disable_source(u); msg=f"⏸ @{u} выключен"
            else: self.db.enable_source(u); msg=f"▶️ @{u} включён"
            self.send(cid,msg,self.sources_keyboard()); return
        if data.startswith("del:"):
            u=data[4:]; r=self.db.get_source(u)
            if r and r['owner']=='primary':
                self.db.disable_source(u); msg=f"⏸ @{u} отключён. Источник остаётся в основном sources.json, история сохранена."
            else:
                self.db.delete_source(u); msg=f"🗑 @{u} удалён из управляемых источников. История сохранена."
            self.send(cid,msg,self.sources_keyboard()); return

    def run(self):
        if not self.s.bot_token: raise RuntimeError("ADMIN_BOT_TOKEN is empty")
        if not self.s.admin_ids: raise RuntimeError("ADMIN_IDS is empty")
        self.api("getMe"); log.info("Admin bot started admins=%s",self.s.admin_ids)
        while True:
            try:
                updates=self.api("getUpdates",{"offset":self.offset,"timeout":self.s.poll_seconds,"allowed_updates":["message","callback_query"]},timeout=self.s.poll_seconds+10) or []
                for u in updates:
                    self.offset=u["update_id"]+1
                    if "callback_query" in u:self.handle_callback(u["callback_query"])
                    elif "message" in u:self.handle_message(u["message"])
            except KeyboardInterrupt: break
            except Exception: log.exception("admin polling failed"); time.sleep(3)
