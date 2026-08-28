# News Bot Admin Addon v1.1.0

Единый control-plane для источников уже работающего News Bot. Основной код и сервис можно оставить без изменений.

## Архитектура

`managed_sources` — единый registry всех источников. Основные источники (`owner=primary`) импортируются из `sources.json`; новые источники (`owner=addon`) добавляются через Telegram. Hot Worker мониторит **все включённые источники**. Старый collector может продолжать работать параллельно: уникальность `(source, source_message_id)` в SQLite не допускает двойной ingest.

При отключении источник блокируется SQLite-trigger'ом на уровне `messages`, поэтому неизменённый primary collector не сможет записывать новые сообщения отключённого источника. История сохраняется.

## Возможности

- Telegram Admin Bot с whitelist `ADMIN_IDS`;
- общая статистика за 24 часа и 7 дней;
- статистика по каждому источнику;
- единый список всех источников;
- enable/disable без перезапуска основного News Bot;
- добавление новых Telegram-источников без остановки;
- hot worker для новых и существующих источников;
- безопасный soft-delete: primary остаётся tombstone, addon отключается;
- SQLite WAL.

## Конфигурация

Создать `/opt/news-bot-v1/.env.admin`:

```env
ADMIN_BOT_TOKEN=...
ADMIN_IDS=123456789
ADMIN_TG_SESSION=runtime/telegram/newsbot_admin

# Необязательно: если хотим отдельную MAX-сессию для hot worker
ADMIN_MAX_SESSION=runtime/max-admin/max-admin.db
ADMIN_MAX_WORK_DIR=runtime/max-admin
ADMIN_MAX_PHONE=+...

ADMIN_HOT_BACKFILL_LIMIT=5
ADMIN_HOT_POLL_SECONDS=5
```

`TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `DB_FILE`, `SOURCE_CONFIG`, LLM и MAX параметры берутся из основного `.env`, если не переопределены в `.env.admin`.

## Запуск

```bash
cd /opt/news-bot-v1
source .venv/bin/activate
set -a; source .env.admin; set +a
PYTHONPATH=/opt/news-bot-v1/admin-addon:/opt/news-bot-v1 python -m newsbot_admin.main
```

## systemd

```bash
cp admin-addon/systemd/newsbot-admin.service /etc/systemd/system/newsbot-admin.service
systemctl daemon-reload
systemctl enable --now newsbot-admin
```

Основной `newsbot` при этом не перезапускается.
