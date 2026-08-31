import asyncio
import logging
import time
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError

log = logging.getLogger("telegram")

RECONNECT_DELAY = 10
HEALTHCHECK_TIMEOUT = 20

# Максимальное время, которое один источник может занимать в одном poll.
SOURCE_TIMEOUT = 45

# После FloodWait источник временно исключается из polling.
# Реальный seconds от Telegram используется напрямую.
SOURCE_COOLDOWN_MIN = 10

# Небольшой защитный cooldown после прочих ошибок.
ERROR_COOLDOWN = 5


class TelegramCollector:
    """
    Безопасный Telegram polling collector.

    Каждый source изолирован:
      - отдельная asyncio task;
      - собственный timeout;
      - собственный FloodWait cooldown;
      - ошибка одного source не останавливает остальные.

    Telegram client при этом общий, поскольку Telethon сам является
    async-клиентом и поддерживает конкурентные запросы.
    """

    def __init__(self, s, db, callback):
        self.s = s
        self.db = db
        self.callback = callback

        self.client = TelegramClient(
            s.tg_session_path,
            s.tg_api_id,
            s.tg_api_hash,

            # КРИТИЧНО:
            # Telethon не должен самостоятельно sleep'ать внутри
            # GetHistoryRequest. Нам нужен FloodWaitError, чтобы
            # изолировать cooldown конкретного source.
            flood_sleep_threshold=0,

            # Не даём одному RPC бесконечно ретраиться внутри Telethon.
            request_retries=1,
            connection_retries=3,
        )

        self.stop_event = asyncio.Event()

        self.cycles = 0
        self.errors = 0

        self.last_cycle_started = None
        self.last_cycle_finished = None
        self.last_success = None
        self.last_error = ""

        self._connected = False

        # username -> monotonic timestamp.
        # Пока now < value, source пропускается.
        self._source_cooldowns = {}

        # username -> currently running task.
        self._source_tasks = {}

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

    def _cooldown_active(self, username):
        until = self._source_cooldowns.get(username, 0.0)
        return time.monotonic() < until

    def _set_cooldown(self, username, seconds, reason):
        seconds = max(
            SOURCE_COOLDOWN_MIN,
            int(seconds),
        )

        until = time.monotonic() + seconds

        self._source_cooldowns[username] = until

        log.warning(
            "poll source=%s cooldown=%ss reason=%s",
            username,
            seconds,
            reason,
        )

    def _clear_cooldown(self, username):
        self._source_cooldowns.pop(username, None)

    async def start(self):
        log.info(
            "Telegram polling started interval=%ss "
            "source_timeout=%ss flood_sleep_threshold=0",
            self.s.poll_interval,
            SOURCE_TIMEOUT,
        )

        try:
            if not await self._connect():
                return

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

                    # ВАЖНО:
                    # disconnect только при реально критической ошибке
                    # самого общего Telegram connection.
                    await self._disconnect()

                    if not await self._connect():
                        break

                if self.stop_event.is_set():
                    break

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
            await self.stop()

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

        tasks = []

        for source in sources:

            if self.stop_event.is_set():
                break

            username = (
                str(source["username"])
                .lstrip("@")
                .lower()
            )

            if not username:
                continue

            # Источник ещё находится в cooldown.
            if self._cooldown_active(username):
                log.info(
                    "poll source=%s skipped cooldown",
                    username,
                )
                continue

            # Дополнительная защита от повторного запуска
            # одного source одновременно.
            existing = self._source_tasks.get(username)

            if existing and not existing.done():
                log.warning(
                    "poll source=%s skipped already_running",
                    username,
                )
                continue

            task = asyncio.create_task(
                self._poll_source_isolated(source),
                name=f"telegram-source:{username}",
            )

            self._source_tasks[username] = task
            tasks.append((username, task))

        if not tasks:
            return

        # Каждый source уже имеет собственный timeout/error boundary.
        # gather дополнительно защищает сам цикл.
        results = await asyncio.gather(
            *(task for _, task in tasks),
            return_exceptions=True,
        )

        for (username, _), result in zip(tasks, results):

            if isinstance(result, asyncio.CancelledError):
                continue

            if isinstance(result, Exception):
                log.error(
                    "poll source=%s isolated task error=%r",
                    username,
                    result,
                )

    async def _poll_source_isolated(self, source):
        username = (
            str(source["username"])
            .lstrip("@")
            .lower()
        )

        try:
            await asyncio.wait_for(
                self.poll_source(source),
                timeout=SOURCE_TIMEOUT,
            )

            self._clear_cooldown(username)

        except asyncio.CancelledError:
            raise

        except FloodWaitError as exc:
            seconds = max(
                SOURCE_COOLDOWN_MIN,
                int(
                    getattr(
                        exc,
                        "seconds",
                        SOURCE_COOLDOWN_MIN,
                    )
                ),
            )

            self.errors += 1
            self.last_error = repr(exc)

            self.db.set_state(
                "poll_errors",
                str(self.errors),
            )

            self._set_cooldown(
                username,
                seconds,
                "FloodWait",
            )

        except asyncio.TimeoutError:
            self.errors += 1
            self.last_error = (
                f"source timeout: {username}"
            )

            self.db.set_state(
                "poll_errors",
                str(self.errors),
            )

            self._set_cooldown(
                username,
                SOURCE_COOLDOWN_MIN,
                "timeout",
            )

            log.error(
                "poll source=%s timeout=%ss; "
                "source isolated",
                username,
                SOURCE_TIMEOUT,
            )

        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)

            self.db.set_state(
                "poll_errors",
                str(self.errors),
            )

            self._set_cooldown(
                username,
                ERROR_COOLDOWN,
                "error",
            )

            log.exception(
                "poll source=%s failed isolated: %s",
                username,
                exc,
            )

        finally:
            current = asyncio.current_task()

            if self._source_tasks.get(username) is current:
                self._source_tasks.pop(username, None)

    async def poll_source(self, source):
        username = (
            str(source["username"])
            .lstrip("@")
            .lower()
        )

        log.debug(
            "poll source=%s started",
            username,
        )

        # Даже get_entity теперь находится внутри
        # _poll_source_isolated и потому тоже изолирован.
        entity = await self.client.get_entity(
            username
        )

        watermark = int(
            source["last_message_id"] or 0
        )

        new_count = 0
        max_id = watermark

        # Получаем только сообщения новее watermark.
        # reverse=True  от старых к новым.
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

            # Пустое сообщение всё равно двигает watermark.
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

        # Сначала отменяем source tasks.
        tasks = [
            task
            for task in self._source_tasks.values()
            if not task.done()
        ]

        if tasks:
            log.info(
                "Cancelling source tasks count=%s",
                len(tasks),
            )

            for task in tasks:
                task.cancel()

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        self._source_tasks.clear()

        await self._disconnect()
