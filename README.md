# ИнфоТочка — News Bot v1

Новостной pipeline для публикации в MAX: **Telegram MTProto → SQLite → дедупликация/event clustering → Groq → Mistral fallback → редакторская проверка → PyMax**.

## Возможности

- мониторинг нескольких публичных Telegram-каналов в реальном времени;
- ID-based routing для `Telethon.NewMessage`;
- safety-net polling, чтобы не терять live-посты;
- heartbeat каждые 60 секунд с состоянием мониторинга;
- startup backfill последних сообщений только за период `RETENTION_DAYS`;
- backfill никогда автоматически не публикуется в MAX;
- SQLite retention cleanup — база хранит только свежую историю;
- exact/lexical dedup и event clustering;
- Groq как основной LLM и Mistral как fallback;
- официальные заявления ведомств/должностных лиц считаются самостоятельным источником и могут публиковаться с явной атрибуцией;
- MAX-публикация контролируется `AUTO_PUBLISH`, priority и confidence.

## Структура

```text
main.py
sources.json
.env.example
requirements.txt
newsbot/
  app.py
  config.py
  db.py
  logging_setup.py
  core/dedup.py
  telegram/collector.py
  llm/adapter.py
  max/publisher.py
deploy/news-bot.service
```

`runtime/`, `.env` и session DB в репозиторий не входят.

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

Для постоянного запуска используйте `deploy/news-bot.service` и настройте `.env`/session DB перед `systemctl enable --now news-bot`.

## Конфигурация публикации

```dotenv
AUTO_PUBLISH=1
MIN_PRIORITY_PUBLISH=3
MIN_CONFIDENCE_PUBLISH=0.50
```

Текущий редактор работает с priority в диапазоне 0–10 по смыслу prompt. Порог `3` позволяет пропускать обычные новости уровня 3 и выше. Confidence `0.50` не требует второго независимого источника для каждого сообщения.

**Важно:** одно только отсутствие второго источника не должно блокировать официальное заявление. Если, например, Минобороны РФ заявило о событии, редактор должен сохранить атрибуцию: «Минобороны РФ заявило…», а не выдавать заявление за независимо подтверждённый факт.

## Startup backfill

При старте collector сначала вооружает live-monitoring и устанавливает watermark, после чего запускает backfill. Поэтому старые сообщения не превращаются в ложные live-публикации.

`RETENTION_DAYS=4` означает, что startup backfill рассматривает только последние четыре дня. `BACKFILL_LIMIT` ограничивает число сообщений на источник. Все backfill-сообщения проходят обработку и попадают в БД, но `backfill=True` гарантирует `publish SKIP`.

## Live monitoring

В логах должны появляться примерно такие строки:

```text
Telegram LIVE monitor armed sources=...
Telegram LIVE watermark source=...
Telegram startup complete ...
Telegram HEARTBEAT connected=True ...
Telegram LIVE received source=... message_id=...
Telegram LIVE poll source=... message_id=... queued=1
```

`NewMessage` — основной путь. Polling каждые `POLL_INTERVAL` секунд — резервный путь.

## Дедупликация

1. exact hash нормализованного текста;
2. lexical similarity с недавними сообщениями;
3. объединение похожих сообщений в `news_event`;
4. повторная публикация одного события блокируется.

## Источники

Источники задаются в `sources.json`:

```json
{
  "username": "some_channel",
  "enabled": true,
  "priority": 10,
  "category": "politics",
  "reliability": 0.95
}
```

Если два username разрешаются Telegram в одну сущность, collector использует один entity ID для live routing и пишет предупреждение о collision.

## Безопасность

Никогда не коммитьте:

- `.env`;
- Telegram `.session`;
- MAX session DB;
- `runtime/`;
- API keys.

После публикации ключей в чат/репозиторий их следует заменить.
