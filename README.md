# ИнфоТочка — News Bot v1.1.0

Production-новостной pipeline для публикации в MAX:

**Telegram MTProto → SQLite → дедупликация / event clustering → Groq → Mistral fallback → редакторская проверка → PyMax.**

С версии **v1.1.0** проект дополнен отдельным **Admin Control Plane** для мониторинга, статистики и единого управления источниками.

## Возможности

### Основной pipeline

- мониторинг публичных Telegram-каналов;
- ID-based routing для `Telethon.NewMessage`;
- safety-net polling, чтобы не терять live-посты;
- heartbeat каждые 60 секунд с состоянием мониторинга;
- startup backfill последних сообщений только за период `RETENTION_DAYS`;
- backfill никогда автоматически не публикуется в MAX;
- SQLite retention cleanup — база хранит только свежую историю;
- exact / lexical dedup и event clustering;
- Groq как основной LLM и Mistral как fallback;
- официальные заявления ведомств/должностных лиц считаются самостоятельным источником и могут публиковаться с явной атрибуцией;
- MAX-публикация контролируется `AUTO_PUBLISH`, `priority` и `confidence`.

### Admin Control Plane

- отдельный Telegram-бот для администратора;
- статистика за сегодня и за последние 7 дней;
- единый Registry источников в SQLite;
- добавление и удаление источников через Telegram-бота;
- включение / выключение источников без изменения основного pipeline;
- просмотр параметров источника: режим, priority, category, reliability, события и последнее сообщение;
- отдельный Hot Worker для источников, принадлежащих addon;
- сохранение Telegram session между перезапусками;
- отдельный `systemd`-сервис `newsbot-admin.service`.

## Архитектура

Основной pipeline и Admin Control Plane работают как отдельные процессы и не требуют изменения кода основного pipeline для управления источниками.

```text
                         SQLite: runtime/news.db
                                  │
                    managed_sources Registry
                                  │
             ┌────────────────────┴────────────────────┐
             │                                         │
       owner=primary                              owner=addon
             │                                         │
             ▼                                         ▼
      news-bot.service                         newsbot-admin.service
             │                                         │
          main.py                              Admin Bot + Hot Worker
             │                                         │
             └────────────────────┬────────────────────┘
                                  ▼
                             MAX / PyMax
```

`managed_sources` является единым реестром источников. Поля Registry включают состояние `enabled`, `owner`, priority, category, reliability и Telegram entity metadata.

Основной pipeline продолжает работать независимо от Admin Bot. Остановка или перезапуск `newsbot-admin.service` не требует остановки `news-bot.service`.

## Структура

```text
main.py
sources.json
.env.example
requirements.txt
requirements-dev.txt
newsbot/
  app.py
  config.py
  db.py
  logging_setup.py
  core/
    dedup.py
  telegram/
    collector.py
  llm/
    adapter.py
  max/
    publisher.py
admin-addon/
  VERSION
  README.md
  requirements-addon.txt
  newsbot_admin/
    bot.py
    config.py
    control_db.py
    hot_worker.py
    main.py
  systemd/
    newsbot-admin.service
```

`runtime/`, `.env`, Telegram session DB, MAX session DB и `.venv/` в репозиторий не входят.

## Конфигурация

Основной pipeline использует `.env` на сервере. Административный addon использует отдельный `.env.admin`.

Пример основного конфига находится в `.env.example`.

Пример минимальной конфигурации addon:

```dotenv
ADMIN_BOT_TOKEN=...
ADMIN_IDS=...
ADMIN_TG_SESSION=runtime/telegram/newsbot_admin
TG_API_ID=...
TG_API_HASH=...
TG_PHONE=...
DB_FILE=runtime/news.db
SOURCE_CONFIG=sources.json
ADMIN_MAX_SESSION=...
ADMIN_MAX_WORK_DIR=runtime/max-admin
ADMIN_MAX_PHONE=...
```

Секреты и session-файлы никогда не должны попадать в Git.

## Windows

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
copy .env.example .env
notepad .env
notepad sources.json
python main.py
```

При первом запуске Telethon может запросить код входа и пароль 2FA. Session сохраняется в `runtime/telegram/`.

## Linux / сервер

```bash
cd /opt/news-bot-v1
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m py_compile main.py newsbot/*.py newsbot/core/*.py newsbot/telegram/*.py newsbot/llm/*.py newsbot/max/*.py
.venv/bin/python main.py
```

Для production используется `news-bot.service`.

### Admin Control Plane

Установка addon:

```bash
cd /opt/news-bot-v1
source .venv/bin/activate
pip install -r admin-addon/requirements-addon.txt
```

После настройки `.env.admin`:

```bash
set -a
source .env.admin
set +a
PYTHONPATH=/opt/news-bot-v1/admin-addon:/opt/news-bot-v1 \
  python -m newsbot_admin.main
```

Для постоянного запуска используется:

```bash
systemctl enable --now newsbot-admin
```

Проверка:

```bash
systemctl status news-bot --no-pager
systemctl status newsbot-admin --no-pager
```

Логи:

```bash
journalctl -u news-bot -f --no-pager
journalctl -u newsbot-admin -f --no-pager
```

`news-bot.service` — основной pipeline.
`newsbot-admin.service` — Admin Control Plane.

## Конфигурация публикации

```dotenv
AUTO_PUBLISH=1
MIN_PRIORITY_PUBLISH=3
MIN_CONFIDENCE_PUBLISH=0.50
```

Текущий редактор работает с priority в диапазоне 0–10 по смыслу prompt. Порог `3` позволяет пропускать обычные новости уровня 3 и выше. Confidence `0.50` не требует второго независимого источника для каждого сообщения.

**Важно:** одно только отсутствие второго источника не должно блокировать официальное заявление. Если, например, Минобороны РФ заявило о событии, редактор должен сохранить атрибуцию: «Минобороны РФ заявило…», а не выдавать заявление за независимо подтверждённый факт.

## Источники и Registry

Основные источники изначально задаются в `sources.json`:

```json
{
  "username": "some_channel",
  "enabled": true,
  "priority": 10,
  "category": "politics",
  "reliability": 0.95
}
```

При запуске Registry импортирует источники из `sources.json` с `owner=primary`.

Источники, добавленные через Admin Bot, получают `owner=addon` и также попадают в общий `managed_sources` Registry.

Поэтому в админ-боте отображаются **все источники**, независимо от того, были они добавлены в `sources.json` или через Telegram-интерфейс.

### Включение / выключение

Поле `enabled` является общим состоянием источника.

```text
1 = включён
0 = выключен
```

Выключение источника через Admin Bot сохраняется в `runtime/news.db` и блокирует приём новых сообщений от него через Registry-механизм.

### Telegram entity collision

Если два username разрешаются Telegram в одну сущность, collector использует один entity ID для live routing и пишет предупреждение о collision. Такие случаи требуют отдельной проверки, поскольку Telegram entity ID не гарантирует уникальность для нескольких username-алиасов.

## Startup backfill

При старте collector сначала вооружает live-monitoring и устанавливает watermark, после чего запускает backfill. Поэтому старые сообщения не превращаются в ложные live-публикации.

`RETENTION_DAYS=4` означает, что startup backfill рассматривает только последние четыре дня. `BACKFILL_LIMIT` ограничивает число сообщений на источник. Все backfill-сообщения проходят обработку и попадают в БД, но `backfill=True` гарантирует `publish SKIP`.

## Live monitoring

В логах основного pipeline должны появляться примерно такие строки:

```text
Telegram LIVE monitor armed sources=...
Telegram LIVE watermark source=...
Telegram startup complete ...
Telegram HEARTBEAT connected=True ...
Telegram LIVE received source=... message_id=...
Telegram LIVE poll source=... message_id=... queued=1
```

`NewMessage` — основной live-путь. Polling через `POLL_INTERVAL` — safety-net, предназначенный для случаев, когда live update не дошёл до приложения.

Heartbeat показывает состояние мониторинга, включая число источников, состояние backfill и счётчики live/poll сообщений.

## Дедупликация

1. exact hash нормализованного текста;
2. lexical similarity с недавними сообщениями;
3. объединение похожих сообщений в `news_event`;
4. повторная публикация одного события блокируется.

## Статистика

Admin Bot предоставляет оперативную статистику по pipeline, включая показатели за текущий день и последние 7 дней.

Для диагностики основного процесса:

```bash
journalctl -u news-bot -n 200 --no-pager
```

Для live-диагностики:

```bash
journalctl -u news-bot -f --no-pager
```

Полезные показатели heartbeat:

```text
connected=True
sources=12
backfill_running=False
backfill_done=12
live_received=...
live_queued=...
live_ignored=...
poll_queued=...
last_live=...
```

## Безопасность

Никогда не коммитьте:

- `.env`;
- `.env.admin`;
- Telegram `.session`;
- MAX session DB;
- `runtime/`;
- API keys;
- другие production credentials.

После публикации ключей в чат или репозиторий их следует заменить.

## Версия

Текущий snapshot production-окружения зафиксирован в Git как **v1.1.0**.
