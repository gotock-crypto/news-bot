import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from ..config import load_sources

log = logging.getLogger("telegram")

HEARTBEAT_SECONDS = 60
DEFAULT_LIVE_POLL_SECONDS = 8
POLL_BATCH_SIZE = 20
POLL_MIN_DELAY = 0.15
BACKFILL_RETENTION_DAYS = 4
CONTROL_SYNC_SECONDS = 3
DYNAMIC_RESOLVE_RETRY_SECONDS = 30


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
        self._poll_interval = max(3.0, float(getattr(settings, "poll_interval", DEFAULT_LIVE_POLL_SECONDS)))
        self._poll_cursor = 0
        self._poll_flood_until = {}
        self._poll_cycles = 0
        self._poll_errors = 0
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
        self._control_sync_task = None
        self._active_state = {x["username"].lower(): bool(x.get("enabled", True)) for x in self.sources}
        self._control_last_sync = None
        self._control_sync_errors = 0
        self._live_registered_ids = set()
        self._resolve_retry_at = {}

    def _read_control_registry(self):
        """Read the complete live source registry from the shared control DB."""
        try:
            with sqlite3.connect(self.s.db_path, timeout=1.5) as c:
                c.row_factory = sqlite3.Row
                rows = c.execute(
                    "SELECT username, enabled, priority, category, reliability, owner, "
                    "telegram_entity_id, title, last_message_id "
                    "FROM managed_sources"
                ).fetchall()
            return {str(r["username"]).lstrip("@").lower(): r for r in rows}
        except (sqlite3.Error, OSError):
            return None

    async def _register_live_entity(self, entity_id, entity):
        """Register a newly discovered entity without restarting the client."""
        if entity_id in self._live_registered_ids:
            return
        self.client.add_event_handler(
            self.on_new,
            events.NewMessage(chats=[entity]),
        )
        self._live_registered_ids.add(entity_id)
        log.info(
            "Telegram LIVE monitor added source_entity id=%s title=%s",
            entity_id,
            getattr(entity, "title", None),
        )

    async def _resolve_dynamic_source(self, source, row):
        username = source["username"]
        now = asyncio.get_running_loop().time()
        retry_at = self._resolve_retry_at.get(username, 0.0)
        if now < retry_at:
            return False
        try:
            entity = None
            entity_id = row["telegram_entity_id"]
            if entity_id:
                try:
                    entity = await self.client.get_entity(int(entity_id))
                except Exception:
                    entity = None
            if entity is None:
                entity = await self.client.get_entity(username)
                entity_id = getattr(entity, "id", None)
            if entity_id is None:
                raise RuntimeError("Telegram entity has no id")

            source["telegram_entity_id"] = int(entity_id)
            self.source_by_id[int(entity_id)] = source
            self.entity_by_id[int(entity_id)] = entity
            self.source_map[username] = source
            self.aliases_by_id.setdefault(int(entity_id), []).append(username)

            latest = await self.client.get_messages(entity, limit=1)
            watermark = latest[0].id if latest else 0
            self._last_seen_by_source[username] = max(
                int(row["last_message_id"] or 0), int(watermark)
            )
            await self._register_live_entity(int(entity_id), entity)
            self._resolve_retry_at.pop(username, None)
            log.info(
                "Telegram dynamic source added username=%s owner=%s enabled=%s id=%s watermark=%s",
                username, source.get("owner"), self._active_state.get(username, True),
                entity_id, self._last_seen_by_source[username],
            )
            return True
        except FloodWaitError as exc:
            seconds = max(1, int(getattr(exc, "seconds", DYNAMIC_RESOLVE_RETRY_SECONDS)))
            self._resolve_retry_at[username] = now + seconds
            log.warning(
                "Telegram dynamic source resolve flood_wait source=%s seconds=%s",
                username, seconds,
            )
        except Exception as exc:
            self._resolve_retry_at[username] = now + DYNAMIC_RESOLVE_RETRY_SECONDS
            log.warning(
                "Telegram dynamic source resolve failed source=%s retry_in=%ss: %s",
                username, DYNAMIC_RESOLVE_RETRY_SECONDS, exc,
            )
        return False

    async def _refresh_control_state(self):
        registry = await asyncio.to_thread(self._read_control_registry)
        if registry is None:
            return False

        changed = []
        # First merge registry rows into the in-memory source list. This is the
        # missing piece that makes sources added by the admin addon visible to
        # the primary collector without a restart.
        for username, row in registry.items():
            source = self.source_map.get(username)
            if source is None:
                source = {
                    "username": username,
                    "enabled": bool(row["enabled"]),
                    "priority": int(row["priority"]),
                    "category": str(row["category"]),
                    "reliability": float(row["reliability"]),
                    "owner": str(row["owner"] or "addon"),
                    "telegram_entity_id": row["telegram_entity_id"],
                    "title": row["title"],
                }
                self.sources.append(source)
                self.source_map[username] = source
                self._active_state[username] = bool(row["enabled"])
                log.info(
                    "Telegram source discovered from control registry source=%s owner=%s enabled=%s",
                    username, source["owner"], bool(row["enabled"]),
                )
                # Resolve/register asynchronously below.
            else:
                source["owner"] = str(row["owner"] or source.get("owner", "primary"))
                source["telegram_entity_id"] = row["telegram_entity_id"] or source.get("telegram_entity_id")
                source["title"] = row["title"] or source.get("title")

            enabled = bool(row["enabled"])
            old = self._active_state.get(username, bool(source.get("enabled", True)))
            self._active_state[username] = enabled
            source["enabled"] = enabled
            source["priority"] = int(row["priority"])
            source["category"] = str(row["category"])
            source["reliability"] = float(row["reliability"])
            if old != enabled:
                changed.append((username, enabled))

        # Any primary config source missing from the registry keeps its local
        # enabled state; the addon seeds all configured primaries at startup.
        for source in self.sources:
            username = source["username"].lstrip("@").lower()
            self._active_state.setdefault(username, bool(source.get("enabled", True)))

        self._control_last_sync = datetime.now(timezone.utc)
        for username, enabled in changed:
            log.info(
                "Telegram source control changed source=%s enabled=%s",
                username, enabled,
            )

        # Resolve only sources that are not yet wired into the live/poll tables.
        # Disabled addon sources are also resolved so that enabling them later is
        # instantaneous; the watermark prevents replaying old history.
        for username, row in registry.items():
            if username not in self.source_map:
                continue
            source = self.source_map[username]
            entity_id = row["telegram_entity_id"]
            if entity_id is None or int(entity_id) not in self.source_by_id:
                await self._resolve_dynamic_source(source, row)

        return True

    def _source_active(self, source):
        return bool(self._active_state.get(
            source["username"].lstrip("@").lower(),
            bool(source.get("enabled", True)),
        ))

    def _effective_source(self, source):
        # Priority/category/reliability are refreshed into the source dict by
        # _refresh_control_state(), so no SQLite query is needed per message.
        return source

    async def _control_sync_loop(self):
        while not self._stopping:
            try:
                ok = await self._refresh_control_state()
                if not ok:
                    self._control_sync_errors += 1
                    log.warning(
                        "Telegram control registry unavailable; keeping last known source state"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._control_sync_errors += 1
                log.exception("Telegram control state refresh failed")
            await asyncio.sleep(CONTROL_SYNC_SECONDS)

    async def start(self):
        if not self.s.tg_api_id or not self.s.tg_api_hash:
            raise RuntimeError("TG_API_ID/TG_API_HASH are required")

        await self.client.start(phone=self.s.tg_phone or None)
        self._started_at = datetime.now(timezone.utc)

        # Resolve the static primary configuration first. The shared registry is
        # merged immediately after, which can then discover addon-only sources.
        await self._resolve_sources()
        await self._refresh_control_state()

        log.info(
            "Telegram connected; sources=%s resolved=%s",
            list(self.source_map),
            len(self.source_by_id),
        )

        self._control_sync_task = asyncio.create_task(
            self._control_sync_loop(),
            name="telegram-control-sync",
        )

        # Register LIVE before touching history.
        if self.source_by_id:
            self._live_handler = self.on_new
            self.client.add_event_handler(
                self._live_handler,
                events.NewMessage(chats=list(self.entity_by_id.values())),
            )
            self._live_registered_ids.update(self.entity_by_id.keys())
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
        cutoff = datetime.now(timezone.utc).timestamp() - BACKFILL_RETENTION_DAYS * 86400
        try:
            for source in self.sources:
                username = source["username"]
                if not self._source_active(source):
                    self._backfill_done += 1
                    log.info("backfill skipped: %s reason=disabled_by_admin", username)
                    continue
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
                        username, BACKFILL_RETENTION_DAYS, processed,
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
        if not self._source_active(source):
            self._live_ignored += 1
            log.info("Telegram LIVE ignored source=%s reason=disabled_by_admin", username)
            return
        source = self._effective_source(source)
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
        """Safety-net poller with round-robin scheduling and per-source backoff.

        The old implementation queried every source in a tight batch every 5s.
        With 12 sources that created a steady stream of GetHistoryRequest calls
        and Telegram flood-waits.  This version makes one history request per
        source per cycle, rotates fairly through all sources, fetches a larger
        burst window, and temporarily skips only the source that was rate-limited.
        """
        while not self._stopping:
            cycle_started = asyncio.get_running_loop().time()
            sources = [
                item for item in self.source_by_id.items()
                if self._source_active(item[1])
            ]
            if not sources:
                await asyncio.sleep(self._poll_interval)
                continue

            # Round-robin cursor prevents one source from becoming the permanent
            # last-polled source when another source takes a flood-wait.
            count = len(sources)
            ordered = [sources[(self._poll_cursor + i) % count] for i in range(count)]
            self._poll_cursor = (self._poll_cursor + 1) % count
            self._poll_cycles += 1

            for entity_id, source in ordered:
                if self._stopping:
                    break

                username = source["username"]
                if not self._source_active(source):
                    log.debug("Telegram LIVE poll skip source=%s reason=disabled_by_admin", username)
                    continue
                source = self._effective_source(source)
                now = asyncio.get_running_loop().time()
                flood_until = self._poll_flood_until.get(username, 0.0)
                if now < flood_until:
                    log.debug(
                        "Telegram LIVE poll skip source=%s reason=flood_backoff remaining=%.1fs",
                        username, flood_until - now,
                    )
                    continue

                try:
                    # Fetch enough messages to survive a short burst between
                    # polls.  Watermarking guarantees already-seen messages are
                    # not sent to the DB/LLM again.
                    latest = await self.client.get_messages(
                        self.entity_by_id[entity_id],
                        limit=POLL_BATCH_SIZE,
                    )
                    if latest:
                        new_messages = []
                        last_seen = self._last_seen_by_source.get(username, 0)
                        for message in reversed(list(latest)):
                            message_id = getattr(message, "id", None)
                            if not message_id or message_id <= last_seen:
                                continue
                            new_messages.append(message)

                        for message in new_messages:
                            message_id = message.id
                            # Advance watermark before processing.  This prevents
                            # a duplicate if the next poll starts immediately.
                            self._last_seen_by_source[username] = max(
                                self._last_seen_by_source.get(username, 0),
                                message_id,
                            )
                            self._last_poll = datetime.now(timezone.utc)
                            self._last_poll_source = username
                            self._last_poll_message_id = message_id

                            queued = await self._process(
                                message,
                                source,
                                backfill=False,
                            )
                            log.info(
                                "Telegram LIVE poll source=%s message_id=%s queued=%s",
                                username,
                                message_id,
                                int(bool(queued)),
                            )
                            if queued:
                                self._poll_queued += 1

                except FloodWaitError as exc:
                    seconds = max(1, int(getattr(exc, "seconds", 5)))
                    # Back off only this source. Other channels continue being
                    # checked instead of the entire poll loop sleeping.
                    self._poll_flood_until[username] = (
                        asyncio.get_running_loop().time() + seconds
                    )
                    log.warning(
                        "Telegram LIVE poll flood_wait source=%s seconds=%s; "
                        "backing off source only",
                        username,
                        seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._poll_errors += 1
                    log.exception(
                        "Telegram LIVE poll failed source=%s",
                        username,
                    )

                # Tiny cooperative pause prevents a burst of requests when the
                # API answers immediately, without materially increasing latency.
                if POLL_MIN_DELAY:
                    await asyncio.sleep(POLL_MIN_DELAY)

            elapsed = asyncio.get_running_loop().time() - cycle_started
            await asyncio.sleep(max(0.0, self._poll_interval - elapsed))

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
                "last_poll_source=%s last_poll_message_id=%s "
                "poll_interval=%.1fs poll_cycles=%s poll_errors=%s flood_backoff_sources=%s "
                "active_sources=%s primary_sources=%s addon_sources=%s control_sync_errors=%s",
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
                self._poll_interval,
                self._poll_cycles,
                self._poll_errors,
                sum(1 for until in self._poll_flood_until.values() if until > asyncio.get_running_loop().time()),
                sum(1 for source in self.sources if self._source_active(source)),
                sum(1 for source in self.sources if source.get("owner", "primary") == "primary"),
                sum(1 for source in self.sources if source.get("owner", "primary") == "addon"),
                self._control_sync_errors,
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
            self._control_sync_task,
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
        if not self._source_active(source):
            return False
        source = self._effective_source(source)
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
