import asyncio
import json
import logging
from typing import Optional

import requests

log = logging.getLogger("telegram_publisher")


class TelegramPublisher:
    """Publish already-rendered news to admin-configured Telegram destinations.

    Uses the existing Admin Bot token. It never touches the Telethon collector
    session and is intentionally best-effort: one destination failing must not
    affect MAX or other destinations.
    """

    def __init__(self, s, db):
        self.s = s
        self.db = db
        self.token = s.admin_bot_token
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.http = requests.Session()

    @property
    def enabled(self):
        return bool(self.token)

    def _api_sync(self, method, payload=None, timeout=25):
        if not self.base:
            raise RuntimeError("Telegram Bot token is not configured")
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

    async def _api(self, method, payload=None, timeout=25):
        return await asyncio.to_thread(self._api_sync, method, payload, timeout)

    @staticmethod
    def _body(title, text):
        return f"{(title or '').strip()}\n\n{(text or '').strip()}".strip()

    async def _send(self, destination, title, text, reply_to=None):
        payload = {
            "chat_id": destination["chat_id"],
            "text": self._body(title, text),
            "disable_web_page_preview": False,
        }
        thread_id = destination["thread_id"]
        if thread_id is not None:
            payload["message_thread_id"] = int(thread_id)
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": int(reply_to)}
        return await self._api("sendMessage", payload)

    async def publish_article(self, eid, article_id, kind, title, text):
        if not self.enabled:
            return

        destinations = self.db.list_telegram_destinations(True)
        if not destinations:
            return

        for destination in destinations:
            existing = self.db.telegram_delivery(destination["id"], article_id)
            if existing and existing["status"] == "sent":
                continue

            parent = None
            if kind == "UPGRADE":
                parent = self.db.last_telegram_delivery(destination["id"], eid)

            parent_id = parent["telegram_message_id"] if parent else None
            parent_delivery_id = parent["id"] if parent else None

            try:
                result = await self._send(
                    destination,
                    title,
                    text,
                    reply_to=parent_id,
                )
                remote_id = int(result["message_id"])
                self.db.record_telegram_delivery(
                    destination["id"],
                    article_id,
                    kind,
                    "sent",
                    telegram_message_id=remote_id,
                    parent_delivery_id=parent_delivery_id,
                )
                log.info(
                    "Telegram publish destination=%s article=%s kind=%s message=%s reply_to=%s",
                    destination["id"], article_id, kind, remote_id, parent_id,
                )
            except Exception as exc:
                self.db.record_telegram_delivery(
                    destination["id"],
                    article_id,
                    kind,
                    "failed",
                    parent_delivery_id=parent_delivery_id,
                    error=str(exc)[:1000],
                )
                log.exception(
                    "Telegram publish failed destination=%s article=%s kind=%s error=%s",
                    destination["id"], article_id, kind, exc,
                )

    async def close(self):
        try:
            self.http.close()
        except Exception:
            pass
