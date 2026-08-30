import asyncio
import logging
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError

log = logging.getLogger("telegram")

RECONNECT_DELAY = 10
HEALTHCHECK_TIMEOUT = 20


class TelegramCollector:
    """Polling-only Telegram collector with reconnect and health checks."""

    def __init__(self, s, db, callback):
        self.s = s
        self.db = db
        self.callback = callback

        self.client = TelegramClient(
            s.tg_session_path,
            s.tg_api_id,
            s.tg_api_hash,
        )

        self.stop_event = asyncio.Event()
        self.cycles = 0
        self.errors = 0

        self.last_cycle_started = None
        self.last_cycle_finished = None
        self.last_success = None
        self.last_error = ""

        self._connected = False

    async def _disconnect(self):
        try:
            await self.client.disconnect()
        except Exception:
            pass

        self._connected = False

    async def _connect(self):
        """
        Подключение к Telegram с автоматическими повторными попытками.
        Используется существующая Telegram session.
        """

        while not self.stop_event.is_set():
            try:
                if not self.client.is_connected():
                    log.info("Telegram connecting...")
                    await self.client.connect()

                if not await self.client.is_user_authorized():
                    raise RuntimeError(
                        "Telegram session is not authorized"
                    )

                self._connected = True

                log.info("Telegram connected")

                return True

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                self.errors += 1
                self.last_error = repr(exc)
                self._connected = False

                log.exception(
                    "Telegram connection failed "
                    "error=%s retry_in=%ss",
                    exc,
                    RECONNECT_DELAY,
                )

                await self._disconnect()

                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=RECONNECT_DELAY,
                    )
                except asyncio.TimeoutError:
                    pass

        return False

    async def _healthcheck(self):
        """
        Проверяем не только is_connected(), но и реальный API-запрос.
        Это позволяет обнаруживать зависшее/stale Telegram-соединение.
        """

        if not self.client.is_connected():
            self._connected = False
            return False

        try:
            await asyncio.wait_for(
                self.client.get_me(),
                timeout=HEALTHCHECK_TIMEOUT,
            )

            self._connected = True

            return True

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log.warning(
                "Telegram healthcheck failed: %s",
                exc,
            )

            self._connected = False

            await self._disconnect()

            return False

    async def _ensure_connected(self):
        if await self._healthcheck():
            return True

        return await self._connect()

    async def start(self):
        log.info(
            "Telegram polling started interval=%ss",
            self.s.poll_interval,
        )

        try:
            if not await self._connect():
                return

            # Первый poll запускается сразу после подключения.
            while not self.stop_event.is_set():

                self.cycles += 1
                cycle = self.cycles

                self.last_cycle_started = time.time()

                log.info(
                    "poll cycle=%s started",
                    cycle,
                )

                try:
                    if not await self._ensure_connected():

                        if self.stop_event.is_set():
                            break

                        continue

                    await self.poll_once()

                    self.last_cycle_finished = time.time()
                    self.last_success = self.last_cycle_finished

                    duration = (
                        self.last_cycle_finished
                        - self.last_cycle_started
                    )

                    log.info(
                        "poll cycle=%s finished duration=%.2fs",
                        cycle,
                        duration,
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    self.errors += 1
                    self.last_error = repr(exc)

                    log.exception(
                        "poll cycle=%s failed error=%s",
                        cycle,
                        exc,
                    )

                    # Принудительно закрываем соединение.
                    # Следующий цикл поднимет его заново.
                    await self._disconnect()

                    if not await self._connect():
                        break

                if self.stop_event.is_set():
                    break

                # Интервал считается после завершения цикла.
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=max(
                            1,
                            int(self.s.poll_interval),
                        ),
                    )

                except asyncio.TimeoutError:
                    pass

        finally:
            await self._disconnect()

            log.info(
                "Telegram polling stopped cycles=%s errors=%s",
                self.cycles,
                self.errors,
            )

    async def poll_once(self):
        if not await self._ensure_connected():
            raise RuntimeError(
                "Telegram is not connected"
            )

        self.db.set_state(
            "last_poll",
            datetime.now(timezone.utc).isoformat(),
        )

        self.db.set_state(
            "poll_errors",
            str(self.errors),
        )

        sources = self.db.list_sources(True)

        log.info(
            "poll cycle sources=%s",
            len(sources),
        )

        for source in sources:

            if self.stop_event.is_set():
                break

            username = (
                str(source["username"])
                .lstrip("@")
                .lower()
            )

            try:
                await self.poll_source(source)

            except asyncio.CancelledError:
                raise

            except FloodWaitError as exc:
                seconds = max(
                    1,
                    int(
                        getattr(
                            exc,
                            "seconds",
                            10,
                        )
                    ),
                )

                self.errors += 1
                self.last_error = repr(exc)

                self.db.set_state(
                    "poll_errors",
                    str(self.errors),
                )

                log.warning(
                    "poll source=%s FloodWait "
                    "seconds=%s; source skipped",
                    username,
                    seconds,
                )

            except Exception as exc:
                self.errors += 1
                self.last_error = repr(exc)

                self.db.set_state(
                    "poll_errors",
                    str(self.errors),
                )

                log.exception(
                    "poll source=%s failed: %s",
                    username,
                    exc,
                )

    async def poll_source(self, source):
        username = (
            str(source["username"])
            .lstrip("@")
            .lower()
        )

        entity = await self.client.get_entity(
            username
        )

        watermark = int(
            source["last_message_id"] or 0
        )

        new_count = 0
        max_id = watermark

        # Получаем только сообщения новее watermark.
        # reverse=True означает порядок от старых к новым.
        async for msg in self.client.iter_messages(
            entity,
            min_id=watermark,
            reverse=True,
        ):

            if self.stop_event.is_set():
                break

            if not msg or not msg.id:
                continue

            msg_id = int(msg.id)

            max_id = max(
                max_id,
                msg_id,
            )

            text = (
                msg.message or ""
            ).strip()

            created_at = (
                msg.date
                .astimezone(timezone.utc)
                .isoformat()
                if getattr(msg, "date", None)
                else datetime.now(
                    timezone.utc
                ).isoformat()
            )

            url = (
                f"https://t.me/"
                f"{username}/"
                f"{msg_id}"
            )

            # Даже пустое сообщение фиксируем в watermark,
            # чтобы оно не обрабатывалось повторно.
            if not text:

                self.db.update_watermark(
                    username,
                    msg_id,
                )

                continue

            row_id = self.db.insert_message(
                source=username,
                source_id=msg_id,
                created_at=created_at,
                text=text,
                url=url,
                media_path="",
                raw_json="",
                priority=int(
                    source["priority"] or 5
                ),
                reliability=float(
                    source["reliability"] or 0.7
                ),
                category=str(
                    source["category"] or "auto"
                ),
            )

            # ВАЖНО:
            # watermark обновляется через существующий API DB.
            self.db.update_watermark(
                username,
                msg_id,
            )

            if row_id is not None:
                new_count += 1

                await self.callback(
                    row_id,
                    username,
                )

        log.info(
            "poll source=%s new=%s watermark=%s",
            username,
            new_count,
            max_id,
        )

    async def stop(self):
        self.stop_event.set()

        await self._disconnect()