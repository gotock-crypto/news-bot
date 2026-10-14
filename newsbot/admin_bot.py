import json
import logging
import threading
import requests

log = logging.getLogger("admin")


class AdminBot:
    """Admin control bot plus minimal per-chat Telegram destination setup.

    Global ADMIN_IDS get the full control panel. Telegram chat owners/admins do
    not get that panel: they can only bind their own chat (and, for forum
    groups, a specific topic) for publication.
    """

    def __init__(self, s, db):
        self.s = s
        self.db = db
        self.offset = 0
        self.state = {}
        self.http = requests.Session()
        self.base = (
            f"https://api.telegram.org/bot{s.admin_bot_token}"
            if s.admin_bot_token else ""
        )
        self.bot_id = None
        self.bot_username = ""
        self.stop_event = threading.Event()

    def api(self, method, payload=None, timeout=40):
        r = self.http.post(
            f"{self.base}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        return data.get("result")

    def auth(self, uid):
        return uid in self.s.admin_ids

    def send(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = json.dumps(
                {"inline_keyboard": keyboard},
                ensure_ascii=False,
            )
        return self.api("sendMessage", payload)

    def menu(self):
        return [
            [
                {"text": "📊 Статистика", "callback_data": "stats"},
                {"text": "🟢 Статус", "callback_data": "status"},
            ],
            [
                {"text": "Dashboard", "callback_data": "dashboard"},
            ],
            [
                {"text": "📰 Источники", "callback_data": "sources"},
                {"text": "📈 Источники 24ч", "callback_data": "srcstats"},
            ],
            [
                {"text": "📢 Telegram", "callback_data": "telegram"},
                {"text": "Stories", "callback_data": "stories"},
                {"text": "➕ Добавить", "callback_data": "add"},
            ],
        ]

    def source_menu(self):
        rows = []
        for r in self.db.list_sources():
            rows.append([
                {
                    "text": ("🟢 " if r["enabled"] else "🔴 ") + "@" + r["username"],
                    "callback_data": "src:" + r["username"],
                }
            ])
        rows.append([
            {"text": "➕ Добавить", "callback_data": "add"},
            {"text": "⬅️ Меню", "callback_data": "home"},
        ])
        return rows

    def telegram_menu(self):
        rows = []
        for r in self.db.list_telegram_destinations():
            icon = "🟢" if r["enabled"] else "🔴"
            title = r["chat_title"] or r["chat_id"]
            thread = r["thread_name"] or (
                f"topic #{r['thread_id']}" if r["thread_id"] is not None else "обычный чат"
            )
            rows.append([
                {
                    "text": f"{icon} {title} · {thread}",
                    "callback_data": f"tg:{r['id']}",
                }
            ])
        rows.append([{"text": "⬅️ Меню", "callback_data": "home"}])
        return rows

    @staticmethod
    def _is_admin_member(member):
        return bool(member and member.get("status") in {"administrator", "creator"})

    def _chat_member(self, chat_id, user_id):
        return self.api("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def _bot_member(self, chat_id):
        if not self.bot_id:
            me = self.api("getMe")
            self.bot_id = int(me["id"])
            self.bot_username = me.get("username", "")
        return self._chat_member(chat_id, self.bot_id)

    def _ensure_bot_can_publish(self, chat_id, chat_type):
        member = self._bot_member(chat_id)
        if not member or member.get("status") not in {"administrator", "creator"}:
            return False, "Бот должен быть добавлен в чат и назначен администратором."
        if chat_type == "channel" and not member.get("can_post_messages", False):
            return False, "Боту нужно дать право публиковать сообщения в канале."
        if chat_type in {"group", "supergroup"} and member.get("status") == "administrator":
            if member.get("can_send_messages") is False:
                return False, "Боту запрещена отправка сообщений в этом чате."
        return True, ""

    def _setup_group(self, uid, message):
        chat = message["chat"]
        cid = chat["id"]
        member = self._chat_member(cid, uid)
        if not self._is_admin_member(member):
            return self.send(cid, "⛔ Настроить публикацию может только администратор этой группы.")

        ok, reason = self._ensure_bot_can_publish(cid, chat.get("type", ""))
        if not ok:
            return self.send(cid, "❌ " + reason)

        chat_info = self.api("getChat", {"chat_id": cid})
        if chat_info.get("is_forum"):
            self.state[(uid, cid)] = {"mode": "select_topic", "chat": chat_info}
            return self.send(
                cid,
                "📢 Настройка новостей\n\n"
                "Откройте нужный раздел (тему) этой группы и отправьте туда /select.\n"
                "Только администраторы группы могут выполнить настройку.",
            )

        self.db.upsert_telegram_destination(
            cid,
            chat.get("type", "group"),
            chat_info.get("title") or chat.get("title") or str(cid),
            None,
            "",
            uid,
        )
        self.state.pop((uid, cid), None)
        return self.send(cid, "✅ Готово. Новости будут публиковаться в эту группу.")

    def _setup_channel(self, uid, target, private_chat_id):
        try:
            chat = self.api("getChat", {"chat_id": target})
            cid = chat["id"]
        except Exception:
            return self.send(private_chat_id, "❌ Не удалось найти канал. Укажите @username канала или его chat_id.")

        if chat.get("type") != "channel":
            return self.send(private_chat_id, "❌ Указанный объект не является каналом.")

        member = self._chat_member(cid, uid)
        if not self._is_admin_member(member):
            return self.send(private_chat_id, "⛔ Вы не являетесь администратором этого канала.")

        ok, reason = self._ensure_bot_can_publish(cid, "channel")
        if not ok:
            return self.send(private_chat_id, "❌ " + reason)

        self.db.upsert_telegram_destination(
            cid,
            "channel",
            chat.get("title") or str(cid),
            None,
            "",
            uid,
        )
        return self.send(
            private_chat_id,
            "✅ Канал подключён. Новости будут публиковаться туда.",
        )

    def _select_topic(self, uid, message):
        chat = message["chat"]
        cid = chat["id"]
        state = self.state.get((uid, cid))
        if not state or state.get("mode") != "select_topic":
            return self.send(cid, "Сначала отправьте /setup в этой группе.")

        member = self._chat_member(cid, uid)
        if not self._is_admin_member(member):
            return self.send(cid, "⛔ Настроить публикацию может только администратор этой группы.")

        thread_id = message.get("message_thread_id")
        if thread_id is None:
            return self.send(cid, "❌ Это не сообщение внутри темы. Откройте нужный раздел и отправьте /select ещё раз.")

        # Telegram does not expose a general get-forum-topic method in the Bot API.
        # The command itself is therefore the authoritative topic selector.
        topic_name = ""
        try:
            topic_name = (message.get("forum_topic_created") or {}).get("name", "")
        except Exception:
            pass

        self.db.upsert_telegram_destination(
            cid,
            chat.get("type", "supergroup"),
            chat.get("title") or str(cid),
            int(thread_id),
            topic_name or f"topic #{thread_id}",
            uid,
        )
        self.state.pop((uid, cid), None)
        return self.send(
            cid,
            "✅ Готово. Новости будут публиковаться в выбранный раздел.",
        )

    def handle(self, update):
        if "callback_query" in update:
            return self.callback(update["callback_query"])

        m = update.get("message")
        if not m:
            return

        uid = m.get("from", {}).get("id")
        cid = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if not text:
            return

        # Minimal public setup surface: non-global chat admins only get these.
        if text == "/select" or text.startswith("/select@"):
            return self._select_topic(uid, m)

        if text == "/setup" or text.startswith("/setup ") or text.startswith("/setup@"):
            chat_type = m["chat"].get("type")
            if chat_type in {"group", "supergroup"}:
                return self._setup_group(uid, m)
            if chat_type == "private":
                parts = text.split(maxsplit=1)
                if len(parts) != 2:
                    return self.send(cid, "Использование: /setup @channel_username\nБот должен быть администратором канала.")
                return self._setup_channel(uid, parts[1].strip(), cid)
            return self.send(cid, "❌ Для канала настройка выполняется в личном чате: /setup @channel_username")

        # Everything else is restricted to the global admin list.
        if not self.auth(uid):
            if text.startswith("/"):
                return self.send(cid, "⛔ Доступ запрещён.")
            return

        if text in ("/start", "/menu"):
            return self.send(cid, "🛠 News Bot — управление", self.menu())
        if text.startswith("/stats"):
            return self.stats(cid)
        if text.startswith("/status"):
            return self.status(cid)
        # Dynamic source management.
        # Changes are written directly to DB and picked up by the
        # collector on the next polling cycle. No service restart needed.

        if text.startswith("/add_source"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return self.send(cid, "Использование: /add_source @channel")

            u = parts[1].strip().split()[0].lstrip("@").lower()

            if not u or not u.replace("_", "").isalnum():
                return self.send(cid, "Некорректный username.")

            self.db.upsert_source(u)
            return self.send(
                cid,
                f"@{u} добавлен/включён. "
                "История не реплеится: первый poll установит watermark.",
                self.source_menu(),
            )

        if text.startswith("/enable_source"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return self.send(cid, "Использование: /enable_source @channel")

            u = parts[1].strip().split()[0].lstrip("@").lower()
            r = self.db.get_source(u)

            if not r:
                return self.send(cid, f"Источник @{u} не найден.")

            self.db.set_source(u, True)

            return self.send(
                cid,
                f"@{u} включён. "
                "Следующий poll продолжит с текущего watermark.",
                self.source_menu(),
            )

        if text.startswith("/disable_source"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return self.send(cid, "Использование: /disable_source @channel")

            u = parts[1].strip().split()[0].lstrip("@").lower()
            r = self.db.get_source(u)

            if not r:
                return self.send(cid, f"Источник @{u} не найден.")

            self.db.set_source(u, False)

            return self.send(
                cid,
                f"@{u} выключен. Watermark сохранён.",
                self.source_menu(),
            )

        if text.startswith("/delete_source"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return self.send(cid, "Использование: /delete_source @channel")

            u = parts[1].strip().split()[0].lstrip("@").lower()
            r = self.db.get_source(u)

            if not r:
                return self.send(cid, f"Источник @{u} не найден.")

            self.db.delete_source(u)

            return self.send(
                cid,
                f"@{u} удалён.",
                self.source_menu(),
            )

        if text == "/stories":
            return self.stories(cid)

        if text.startswith("/story"):
            parts = text.split(maxsplit=1)

            if len(parts) != 2:
                return self.send(
                    cid,
                    "Использование: /story <ID>\nНапример: /story 1321",
                    self.menu(),
                )

            return self.story(cid, parts[1].strip())

        if text.startswith("/sources"):
            return self.send(cid, "📰 Источники", self.source_menu())
        if text.startswith("/telegram"):
            return self.telegram_stats(cid)
        if self.state.get(uid) == "add":
            return self.add(uid, cid, text)
        return self.send(cid, "Выберите действие:", self.menu())

    def stories(self, c):
        rows = self.db.story_candidates(hours=24, limit=50)

        if not rows:
            return self.send(
                c,
                "STORIES\n\nАктивных Stories за последние 24 часа нет.",
                self.menu(),
            )

        lines = ["STORIES  последние 50", ""]

        for r in rows:
            eid = int(r["event_id"])
            title = str(r["title"] or "Без названия")
            status = str(r["status"] or "")
            facts = self.db.story_facts(eid, limit=1000)
            messages = self.db.story_messages(eid, limit=1000)

            lines.append(
                f"#{eid} [{status}]\n"
                f"{title}\n"
                f"facts={len(facts)}  messages={len(messages)}\n"
                f"/story {eid}"
            )

        text = "\n\n".join(lines)

        if len(text) > 3900:
            text = text[:3900] + "\n\n список сокращён"

        return self.send(c, text, self.menu())


    def story(self, c, event_id):
        try:
            event_id = int(event_id)
        except (TypeError, ValueError):
            return self.send(
                c,
                "Использование: /story <ID>\nНапример: /story 1321",
                self.menu(),
            )

        con = self.db.conn()
        try:
            row = con.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
        finally:
            con.close()

        if not row:
            return self.send(
                c,
                f"Story #{event_id} не найдена.",
                self.menu(),
            )

        facts = self.db.story_facts(event_id, limit=1000)
        messages = self.db.story_messages(event_id, limit=1000)

        lines = [
            f"STORY #{event_id}",
            "",
            f"Название: {row['title'] or ''}",
            f"Статус: {row['status'] or ''}",
            f"Создана: {row['created_at'] or ''}",
            f"Обновлена: {row['updated_at'] or ''}",
            "",
            "FACTS:",
        ]

        if facts:
            for i, fact in enumerate(facts, 1):
                lines.append(f"{i}. {fact['fact_text']}")
        else:
            lines.append("нет сохранённых фактов")

        lines.extend(["", "RAW MESSAGES:"])

        if messages:
            for m in messages:
                text = (m["text"] or "").strip() or ""
                lines.append(f"[{m['id']}] {text}")
        else:
            lines.append("нет сообщений")

        text = "\n".join(lines)

        if len(text) > 3900:
            text = text[:3900] + "\n\n содержимое сокращено"

        return self.send(
            c,
            text,
            [
                [
                    {"text": "Stories", "callback_data": "stories"},
                    {"text": "Меню", "callback_data": "home"},
                ]
            ],
        )


    def stats(self, c):
        a = self.db.stats(24)
        b = self.db.stats(168)
        return self.send(
            c,
            f"📊 24 часа\nПолучено: {a['received']}\nСобытий: {a['events']}\n"
            f"Upgrade: {a['upgrades']}\nОпубликовано: {a['published']}\n\n"
            f"📊 7 дней\nПолучено: {b['received']}\nСобытий: {b['events']}\n"
            f"Upgrade: {b['upgrades']}\nОпубликовано: {b['published']}",
            self.menu(),
        )

    def dashboard(self, c):
        x = self.db.dashboard_stats(24)

        jobs = x["jobs"]
        tg = x["telegram"]
        th = x["threads"]
        poll = x["poll"]

        text = (
            "NEWS BOT DASHBOARD\n\n"
            "PROCESSING\n"
            f"Pending: {jobs['pending']}\n"
            f"Processing: {jobs['processing']}\n"
            f"Done: {jobs['done']}\n"
            f"Failed: {jobs['failed']}\n\n"
            "TELEGRAM 24h\n"
            f"Destinations: {tg['destinations']}\n"
            f"Enabled: {tg['enabled']}\n"
            f"Sent: {tg['sent']}\n"
            f"Failed: {tg['failed']}\n"
            f"Pending: {tg['pending']}\n\n"
            "THREADS 24h\n"
            f"Total: {th['total']}\n"
            f"Sent: {th['sent']}\n"
            f"Failed: {th['failed']}\n"
            f"Last activity: {th['last_updated'] or '-'}\n\n"
            "POLLING\n"
            f"Last poll: {poll['last_poll'] or '-'}\n"
            f"Errors: {poll['poll_errors']}"
        )

        return self.send(
            c,
            text,
            [
                [
                    {"text": "Refresh", "callback_data": "dashboard"},
                    {"text": "Menu", "callback_data": "home"},
                ]
            ],
        )

    def telegram_stats(self, c):
        x = self.db.telegram_stats(24)
        lines = [
            "📢 TELEGRAM",
            "",
            f"Destinations: {len(x['destinations'])}",
            f"Enabled: {x['enabled']}",
            f"24ч отправлено: {x['sent']}",
            f"24ч ошибок: {x['failed']}",
            "",
        ]
        for r in x["destinations"]:
            icon = "🟢" if r["enabled"] else "🔴"
            thread = r["thread_name"] or (f"topic #{r['thread_id']}" if r["thread_id"] is not None else "обычный")
            lines.append(
                f"{icon} {r['chat_title'] or r['chat_id']} · {thread}\n"
                f"   sent={int(r['sent'] or 0)} failed={int(r['failed'] or 0)}"
            )
        return self.send(c, "\n".join(lines), self.telegram_menu())

    def status(self, c):
        self.send(
            c,
            f"🟢 NEWS BOT\n\nTelegram: {self.db.get_state('telegram_status', 'unknown')}\n"
            f"Источников: {len(self.db.list_sources(True))}\n"
            f"Последний poll: {self.db.get_state('last_poll', '—')}\n"
            f"Ошибок polling: {self.db.get_state('poll_errors', '0')}\n"
            f"DB: 🟢\nAdmin Bot: 🟢",
            self.menu(),
        )

    def add(self, uid, c, text):
        u = text.split()[0].lstrip("@").lower() if text.split() else ""
        if not u or not u.replace("_", "").isalnum():
            return self.send(c, "❌ Некорректный username.", self.menu())
        self.db.upsert_source(u)
        self.state.pop(uid, None)
        return self.send(c, f"✅ @{u} добавлен. История не реплеится: первый poll установит watermark.", self.source_menu())

    def callback(self, q):
        uid = q.get("from", {}).get("id")
        c = q.get("message", {}).get("chat", {}).get("id")
        d = q.get("data", "")
        if not self.auth(uid):
            return
        try:
            self.api("answerCallbackQuery", {"callback_query_id": q.get("id")})
        except Exception:
            pass
        if d in ("home", "refresh"):
            return self.send(c, "🛠 News Bot — управление", self.menu())
        if d == "stats":
            return self.stats(c)
        if d == "status":
            return self.status(c)
        if d == "dashboard":
            return self.dashboard(c)
        if d == "sources":
            return self.send(c, "📰 Источники", self.source_menu())
        if d == "telegram":
            return self.telegram_stats(c)
        if d == "stories":
            return self.stories(c)
        if d.startswith("story:"):
            return self.story(c, d[6:])

        if d == "srcstats":
            rows = self.db.source_stats(24)
            return self.send(
                c,
                "📈 Источники — 24ч\n\n" + ("\n".join(f"@{r['source']} — {r['received']}" for r in rows) or "Нет сообщений."),
                self.menu(),
            )
        if d == "add":
            self.state[uid] = "add"
            return self.send(c, "➕ Пришлите username канала, например @example_news.")
        if d.startswith("tg:"):
            rid = int(d[3:])
            r = self.db.get_telegram_destination(rid)
            if not r:
                return
            thread = r["thread_name"] or (f"topic #{r['thread_id']}" if r["thread_id"] is not None else "обычный чат")
            k = [[
                {"text": "⏸ Выключить" if r["enabled"] else "▶️ Включить", "callback_data": f"tgtoggle:{rid}"},
                {"text": "🗑 Удалить", "callback_data": f"tgdelete:{rid}"},
            ], [{"text": "⬅️ Telegram", "callback_data": "telegram"}]]
            return self.send(
                c,
                f"📢 {r['chat_title'] or r['chat_id']}\n\n"
                f"Тип: {r['chat_type']}\nРаздел: {thread}\n"
                f"chat_id: {r['chat_id']}\nСтатус: {'🟢' if r['enabled'] else '🔴'}",
                k,
            )
        if d.startswith("tgtoggle:"):
            rid = int(d[9:])
            r = self.db.get_telegram_destination(rid)
            if r:
                self.db.set_telegram_destination(rid, not bool(r["enabled"]))
            return self.telegram_stats(c)
        if d.startswith("tgdelete:"):
            self.db.delete_telegram_destination(int(d[9:]))
            return self.telegram_stats(c)

        if d.startswith("src:"):
            u = d[4:]
            r = self.db.get_source(u)
            if not r:
                return
            k = [[{"text": "⏸ Выключить" if r["enabled"] else "▶️ Включить", "callback_data": "toggle:" + u}]]
            if r["owner"] == "addon":
                k[0].append({"text": "🗑 Удалить", "callback_data": "delete:" + u})
            k.append([{"text": "⬅️ Источники", "callback_data": "sources"}])
            return self.send(
                c,
                f"@{u}\n\nСтатус: {'🟢' if r['enabled'] else '🔴'}\nOwner: {r['owner']}\n"
                f"Priority: {r['priority']}\nCategory: {r['category']}\nReliability: {r['reliability']:.2f}\n"
                f"Watermark: {r['last_message_id']}",
                k,
            )
        if d.startswith("toggle:"):
            u = d[7:]
            r = self.db.get_source(u)
            if r:
                self.db.set_source(u, not bool(r["enabled"]))
            return self.send(c, "Готово.", self.source_menu())
        if d.startswith("delete:"):
            self.db.delete_source(d[7:])
            return self.send(c, "Готово.", self.source_menu())

    def run_forever(self):
        if not self.s.admin_bot_token or not self.s.admin_ids:
            log.warning("Admin Bot disabled")
            return
        self.bot_id = int(self.api("getMe")["id"])
        log.info("Admin Bot started bot_id=%s", self.bot_id)
        while not self.stop_event.is_set():
            try:
                updates = self.api(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": self.s.admin_poll_seconds,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=self.s.admin_poll_seconds + 10,
                ) or []
                for u in updates:
                    self.offset = u["update_id"] + 1
                    try:
                        self.handle(u)
                    except Exception:
                        log.exception("admin update failed")
            except Exception:
                if self.stop_event.is_set():
                    break
                log.exception("admin polling failed")
                import time
                self.stop_event.wait(3)

    def stop(self):
        self.stop_event.set()
