import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events

from ..config import load_sources

log = logging.getLogger("telegram")

HEARTBEAT_SECONDS = 60
DEFAULT_LIVE_POLL_SECONDS = 5
DEFAULT_BACKFILL_RETENTION_DAYS = 4


class TelegramCollector:
    """Telegram source collector.

    Live updates are registered by resolved Telegram entity IDs, not by
    usernames.  This is important for channels where Telethon delivers an
    update without a usable ``event.chat.username``.

    Startup backfill runs in parallel with LIVE monitoring.  The poller is a
    safety net for missed Telegram updates and also gives us explicit proof
    in logs that the sources are being checked.
    """

    def __init__(self, settings, db, callback):
        self.s = settings
        self.db = db
        self.callback = callback

        self.client = TelegramClient(
            settings.tg_session_path,
            settings.tg_api_id,
            settings.tg_api_hash,
        )

        self.sources = load_sources(settings.source_path)
        self.source_map = {
            x["username"].lower(): x for x in self.sources
        }

        # Resolved entity id -> canonical source.
        self.source_by_id = {}
        self.entity_by_id = {}
        self.aliases_by_id = {}

        self._backfill_task = None
        self._heartbeat_task = None
        self._live_poll_task = None
        self._started_at = None
        self._backfill_done = 0
        self._backfill_processed = 0
        self._backfill_running = False
        self._live_received = 0
        self._live_queued = 0
        self._live_ignored = 0
        self._last_live = None
        self._last_poll = None
        self._last_poll_source = None
        self._last_poll_message_id = None
        self._poll_queued = 0
        self._last_seen_by_source = {}
        self._stopping = False
        self._live_handler = None

    async def start(self):
        if not self.s.tg_api_id or not self.s.tg_api_hash:
            raise RuntimeError("TG_API_ID/TG_API_HASH are required")

        await self.client.start(phone=self.s.tg_phone or None)
        self._started_at = datetime.now(timezone.utc)

        await self._resolve_sources()

        log.info(
            "Telegram connected; sources=%s resolved=%s",
            list(self.source_map),
            len(self.source_by_id),
        )

        # Register LIVE before touching history.
        if self.source_by_id:
            self._live_handler = self.on_new
            self.client.add_event_handler(
                self._live_handler,
                events.NewMessage(chats=list(self.entity_by_id.values())),
            )
            log.info(
                "Telegram LIVE monitor armed sources=%s ids=%s",
                len(self.source_by_id),
                list(self.source_by_id),
            )

        # Establish poll watermarks after the LIVE handler is armed. This
        # closes the startup race while preventing old history from becoming
        # fake LIVE messages in the fallback poller.
        await self._initialize_poll_watermarks()

        # History must never block the live stream.
        self._backfill_running = True
        self._backfill_task = asyncio.create_task(
            self.backfill(),
            name="telegram-backfill",
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(),
            name="telegram-heartbeat",
        )
        self._live_poll_task = asyncio.create_task(
            self._live_poll(),
            name="telegram-live-poll",
        )

        try:
            await self.client.run_until_disconnected()
        finally:
            await self._shutdown_tasks()

    async def _resolve_sources(self):
        """Resolve usernames once and build an ID-based routing table."""
        for source in self.sources:
            username = source["username"].lower()
            try:
                entity = await self.client.get_entity(username)
                entity_id = getattr(entity, "id", None)
                if entity_id is None:
                    raise RuntimeError("Telegram entity has no id")

                self.aliases_by_id.setdefault(entity_id, []).append(username)

                if entity_id in self.source_by_id:
                    existing = self.source_by_id[entity_id]
                    log.warning(
                        "Telegram source alias collision id=%s existing=%s alias=%s; "
                        "using existing source for LIVE routing",
                        entity_id,
                        existing["username"],
                        username,
                    )
                    continue

                self.source_by_id[entity_id] = source
                self.entity_by_id[entity_id] = entity
                log.info(
                    "Telegram source resolved username=%s id=%s type=%s",
                    username,
                    entity_id,
                    type(entity).__name__,
                )
            except Exception as exc:
                log.exception(
                    "Telegram source resolve failed username=%s: %s",
                    username,
                    exc,
                )

        for entity_id, aliases in self.aliases_by_id.items():
            if len(aliases) > 1:
                log.warning(
                    "Telegram duplicate entity id=%s aliases=%s",
                    entity_id,
                    aliases,
                )

    async def _initialize_poll_watermarks(self):
        for entity_id, source in list(self.source_by_id.items()):
            username = source["username"]
            try:
                latest = await self.client.get_messages(self.entity_by_id[entity_id], limit=1)
                if latest:
                    self._last_seen_by_source[username] = latest[0].id
                    log.info(
                        "Telegram LIVE watermark source=%s message_id=%s",
                        username,
                        latest[0].id,
                    )
            except Exception:
                log.exception(
                    "Telegram LIVE watermark failed source=%s",
                    username,
                )

    async def backfill(self):
        retention_days = max(1, int(getattr(self.s, "retention_days", DEFAULT_BACKFILL_RETENTION_DAYS)))
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        try:
            for source in self.sources:
                username = source["username"]
                try:
                    entity = await self.client.get_entity(username)
                    processed = 0
                    watermark = self._last_seen_by_source.get(username, 0)

                    async for message in self.client.iter_messages(
                        entity,
                        limit=self.s.backfill_limit,
                    ):
                        if message.date and message.date.timestamp() < cutoff:
                            break
                        # Watermark is deliberately excluded: it is the newest
                        # message seen before backfill started and must not cause
                        # an unnecessary LLM call.
                        if getattr(message, "id", 0) >= watermark:
                            continue
                        try:
                            msg_id = await self._process(message, source, backfill=True)
                            if msg_id:
                                processed += 1
                                self._backfill_processed += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception("backfill message failed %s/%s", username, getattr(message, "id", "?"))

                    self._backfill_done += 1
                    log.info(
                        "backfill done: %s retention_days=%s processed=%s publishing=disabled",
                        username, retention_days, processed,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("backfill failed: %s", username)
        finally:
            self._backfill_running = False
            log.info(
                "Telegram startup backfill finished sources_done=%s/%s processed=%s publishing=disabled",
                self._backfill_done, len(self.sources), self._backfill_processed,
            )
            log.info(
                "Telegram startup complete sources=%s backfill_done=%s backfill_processed=%s live_received=%s",
                len(self.sources), self._backfill_done, self._backfill_processed, self._live_received,
            )

    async def on_new(self, event):
        """Primary real-time Telegram handler."""
        self._live_received += 1
        self._last_live = datetime.now(timezone.utc)

        chat_id = getattr(event, "chat_id", None)
        message = event.message
        source = self.source_by_id.get(chat_id)

        # Fallback for unusual Telethon events where chat_id is unavailable.
        if source is None:
            username = getattr(event.chat, "username", None) or ""
            source = self.source_map.get(username.lower())

        if source is None:
            self._live_ignored += 1
            log.warning(
                "Telegram LIVE ignored chat_id=%s message_id=%s received=%s",
                chat_id,
                getattr(message, "id", "?"),
                self._live_received,
            )
            return

        username = source["username"]
        message_id = getattr(message, "id", None)
        if message_id:
            self._last_seen_by_source[username] = max(
                self._last_seen_by_source.get(username, 0),
                message_id,
            )
        log.info(
            "Telegram LIVE received source=%s chat_id=%s message_id=%s",
            username,
            chat_id,
            message_id,
        )

        try:
            queued = await self._process(
                message,
                source,
                backfill=False,
            )
            if queued:
                self._live_queued += 1
                log.info(
                    "Telegram LIVE queued source=%s message_id=%s queued=%s",
                    username,
                    message_id,
                    self._live_queued,
                )
            else:
                self._live_ignored += 1
                log.info(
                    "Telegram LIVE duplicate/ignored source=%s message_id=%s",
                    username,
                    message_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "live message failed %s/%s",
                username,
                message_id,
            )

    async def _live_poll(self):
        """Safety-net poller for messages missed by Telegram update delivery."""
        while not self._stopping:
            try:
                for entity_id, source in list(self.source_by_id.items()):
                    username = source["username"]
                    entity = self.entity_by_id[entity_id]
                    latest = await self.client.get_messages(entity, limit=3)

                    if not latest:
                        continue

                    # Oldest -> newest so a short burst is processed in order.
                    for message in reversed(list(latest)):
                        if not getattr(message, "id", None):
                            continue
                        last_seen = self._last_seen_by_source.get(username, 0)
                        if message.id <= last_seen:
                            continue
                        self._last_seen_by_source[username] = message.id
                        self._last_poll = datetime.now(timezone.utc)
                        self._last_poll_source = username
                        self._last_poll_message_id = message.id

                        queued = await self._process(
                            message,
                            source,
                            backfill=False,
                        )
                        log.info(
                            "Telegram LIVE poll source=%s message_id=%s queued=%s",
                            username,
                            message.id,
                            int(bool(queued)),
                        )
                        if queued:
                            self._poll_queued += 1

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram LIVE poll failed")

            poll_seconds = max(1, int(getattr(self.s, "poll_interval", DEFAULT_LIVE_POLL_SECONDS)))
            await asyncio.sleep(poll_seconds)

    async def _heartbeat(self):
        while not self._stopping:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if self._stopping:
                break

            connected = bool(self.client.is_connected())
            uptime = "00:00:00"
            if self._started_at:
                seconds = int(
                    (datetime.now(timezone.utc) - self._started_at).total_seconds()
                )
                uptime = f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

            last_live = self._last_live.isoformat() if self._last_live else "never"
            log.info(
                "Telegram HEARTBEAT connected=%s sources=%s "
                "backfill_running=%s backfill_done=%s backfill_processed=%s "
                "live_received=%s live_queued=%s live_ignored=%s poll_queued=%s last_live=%s uptime=%s "
                "last_poll_source=%s last_poll_message_id=%s",
                connected,
                len(self.sources),
                self._backfill_running,
                self._backfill_done,
                self._backfill_processed,
                self._live_received,
                self._live_queued,
                self._live_ignored,
                self._poll_queued,
                last_live,
                uptime,
                self._last_poll_source or "never",
                self._last_poll_message_id or "never",
            )

    async def _shutdown_tasks(self):
        self._stopping = True
        if self._live_handler is not None:
            try:
                self.client.remove_event_handler(self._live_handler)
            except Exception:
                log.exception("Telegram LIVE handler removal failed")
        tasks = [
            self._backfill_task,
            self._heartbeat_task,
            self._live_poll_task,
        ]
        current = asyncio.current_task()
        for task in tasks:
            if task and task is not current and not task.done():
                task.cancel()

        for task in tasks:
            if task and task is not current:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("Telegram shutdown task failed")

        log.info(
            "Telegram shutdown complete live_received=%s live_queued=%s live_ignored=%s poll_queued=%s",
            self._live_received,
            self._live_queued,
            self._live_ignored,
            self._poll_queued,
        )

    async def _process(self, message, source, backfill=False):
        text = (message.message or "").strip()
        if not text:
            return False

        username = source["username"]
        message_id = message.id
        url = f"https://t.me/{username}/{message_id}"
        media_path = ""

        # Media is intentionally not downloaded here.  A live news pipeline
        # must never wait on a large/slow Telegram file.
        raw = json.dumps(
            {
                "source": username,
                "id": message_id,
                "date": message.date.isoformat() if message.date else None,
                "has_media": bool(message.media),
                "backfill": bool(backfill),
            },
            ensure_ascii=False,
        )

        msg_id = self.db.insert_message(
            username,
            message_id,
            message.date.isoformat() if message.date else "",
            text,
            url,
            media_path,
            raw,
            source["priority"],
            source["reliability"],
            source["category"],
        )

        if not msg_id:
            return False

        log.info(
            "telegram article queued source=%s message_id=%s db_id=%s backfill=%s",
            username,
            message_id,
            msg_id,
            backfill,
        )

        # Backfill must never publish.  The App callback gets the explicit
        # flag when supported; keep compatibility with the existing callback
        # signature so the rest of the architecture stays unchanged.
        try:
            await self.callback(msg_id, source, backfill=backfill)
        except TypeError as exc:
            if "backfill" not in str(exc):
                raise
            await self.callback(msg_id, source)

        return True
