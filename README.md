# News Bot v1

Production-пайплайн новостного бота для MAX. Система получает сообщения из публичных Telegram-каналов через пользовательскую MTProto-сессию, сохраняет их в SQLite, объединяет дубликаты и сообщения об одном событии, передаёт материал редактору на базе Groq/Mistral и при прохождении правил публикует готовую новость в MAX через PyMax.

Проект также содержит **Admin Addon v1.1.0** — отдельный control plane для управления источниками, статистики и hot-monitoring без необходимости останавливать основной News Bot.

---

## Возможности

### Основной News Bot

- Telegram MTProto / Telethon collector;
- мониторинг источников по Telegram entity ID после разрешения username;
- обработка новых сообщений в реальном времени;
- safety-net polling, чтобы снизить риск пропуска live-сообщений;
- startup backfill последних сообщений источников;
- backfill никогда не публикуется автоматически в MAX;
- SQLite с WAL-режимом;
- хранение исходного текста, URL, media path и raw JSON;
- нормализация и similarity-based дедупликация;
- объединение сообщений разных источников в единое событие;
- Groq как основной LLM и Mistral как fallback;
- редакторская проверка новости перед публикацией;
- категории `politics`, `svo`, `russia`, `world`, `economy`, `security`;
- автоматическая публикация в MAX по `priority`, `confidence` и `AUTO_PUBLISH`;
- media-first публикация: при наличии локального изображения PyMax может отправить фото вместе с текстом;
- retention cleanup старой истории SQLite.

### Admin Addon v1.1.0

Addon является отдельным control plane поверх уже работающего News Bot.

Он предоставляет Telegram-бота администратора и Hot Worker. Оба используют ту же SQLite-базу `runtime/news.db`, поэтому управление источниками происходит через единый registry `managed_sources`.

Возможности:

- whitelist администраторов через `ADMIN_IDS`;
- Telegram Admin Bot с inline-кнопками;
- статистика за последние 24 часа и 7 дней;
- статистика отдельно по источникам;
- просмотр всех источников, включая выключенные;
- просмотр `priority`, `category`, `reliability`, владельца и состояния источника;
- включение/выключение источника без изменения `sources.json` и без перезапуска основного сервиса;
- добавление нового Telegram-источника прямо из Admin Bot;
- автоматический подхват новых источников Hot Worker'ом;
- общий Registry для `primary` и `addon` источников;
- soft-delete: история сообщений не удаляется;
- SQLite WAL для безопасной совместной работы процессов;
- защита от повторного ingest при параллельной работе primary collector и Hot Worker.

---

## Архитектура

```text
                         Telegram channels
                                │
                                ▼
                    Telegram MTProto / Telethon
                                │
              ┌─────────────────┴─────────────────┐
              │                                   │
       Primary Collector                    Admin Hot Worker
         news-bot.service                  newsbot-admin.service
              │                                   │
              └─────────────────┬─────────────────┘
                                ▼
                       SQLite: runtime/news.db
                                │
                   managed_sources Registry
                                │
                                ▼
                         message storage
                                │
                                ▼
                    dedup / event clustering
                                │
                                ▼
                         Groq / Mistral
                         editorial LLM
                                │
                                ▼
                     priority + confidence
                                │
                                ▼
                           PyMax
                                │
                                ▼
                         MAX publication

                    Admin Bot ───────► Registry
                       │
                       ├── Statistics
                       ├── Sources
                       ├── Enable / Disable
                       └── Add source
```

### Основной процесс

`main.py` создаёт `App`, подключает SQLite, очищает старую историю, запускает LLM и MAX publisher, после чего одновременно запускает Telegram collector и безопасный старт MAX.

Collector:

1. читает базовые источники из `sources.json`;
2. синхронизируется с `managed_sources` в общей БД;
3. разрешает Telegram username в entity ID;
4. регистрирует live handlers по entity ID;
5. делает startup backfill;
6. продолжает получать новые сообщения;
7. периодически проверяет Registry, поэтому изменения `enabled` и новые addon-источники могут подхватываться без перезапуска.

### Admin control plane

Addon запускается отдельным процессом. Admin Bot отвечает за интерфейс управления, а Hot Worker — за live/backfill обработку включённых источников.

Оба процесса используют общий Registry:

```text
managed_sources
├── username
├── enabled
├── priority
├── category
├── reliability
├── owner
├── telegram_entity_id
├── title
├── created_at
├── updated_at
└── last_message_id
```

`owner=primary` означает источник из основного `sources.json`. `owner=addon` означает источник, добавленный через Admin Bot.

---

## Дедупликация и события

Система не считает каждую публикацию Telegram отдельной новостью.

Для сообщения рассчитывается нормализованный SHA-256 hash. При нормализации убираются регистр, различия `ё/е`, URL, пунктуация и лишние пробелы.

Затем выполняется поиск похожих сообщений в заданном временном окне. Используются:

- token/Jaccard similarity;
- последовательностное сходство текста;
- порог `SIMILARITY_THRESHOLD`.

Похожие сообщения могут быть связаны с одним `event_id`. Это позволяет собрать несколько публикаций разных каналов в одно событие и повторно отправить событие в LLM для обновления итогового материала.

Важно: `norm_hash` не является UNIQUE. Одинаковый текст от разных Telegram-источников должен сохраняться, потому что это потенциально несколько источников одного события.

Уникальность одного Telegram-сообщения обеспечивается парой:

```text
(source, source_message_id)
```

---

## Редакторский LLM

LLM получает один или несколько исходных материалов и возвращает строго JSON:

```json
{
  "publish": true,
  "title": "...",
  "text": "...",
  "category": "politics",
  "priority": 0,
  "confidence": 0.0,
  "reason": "..."
}
```

Редакторская политика требует:

- отделять факты от оценок и эмоций;
- не выдумывать имена, цифры, даты, места и цитаты;
- не усиливать исходные формулировки;
- не копировать исходный текст дословно;
- объединять материалы об одном событии;
- учитывать надёжность источника;
- снижать confidence при отсутствии независимого подтверждения;
- явно сохранять атрибуцию официальных заявлений;
- не публиковать рекламу, поздравления, мемы, бытовые посты и очевидный флуд;
- не выдавать неподтверждённое предположение за установленный факт.

Если источники противоречат друг другу, модель не должна самостоятельно придумывать, кто прав.

### Провайдеры

Основной провайдер — Groq через официальный Python SDK. При ошибке используется Mistral через HTTP API.

LLM защищён асинхронным lock и ограничением частоты запросов. Для Groq предусмотрен cooldown после проблем с провайдером.

По умолчанию в snapshot используются:

```env
GROQ_MODEL=qwen/qwen3-27b
MISTRAL_MODEL=mistral-small-latest
```

---

## Правила публикации

Публикация проходит несколько независимых проверок.

### 1. `AUTO_PUBLISH`

```env
AUTO_PUBLISH=0
```

Безопасный режим: сообщения обрабатываются, но автоматически в MAX не отправляются.

```env
AUTO_PUBLISH=1
```

Разрешает автоматическую публикацию материалов, которые прошли редакторскую проверку и пороги.

### 2. Priority

Текущий контракт LLM использует шкалу `0..10`.

В snapshot установлен:

```env
MIN_PRIORITY_PUBLISH=70
```

Код основного pipeline в текущей версии ожидает шкалу LLM `0..10`, поэтому при использовании старого значения `70` оно должно интерпретироваться как legacy-порог `7`.

### 3. Confidence

В snapshot установлен:

```env
MIN_CONFIDENCE_PUBLISH=0.82
```

Материалы с confidence ниже порога не публикуются автоматически.

### 4. Startup backfill

Backfill нужен для заполнения базы свежей историей, но публикация из startup backfill запрещена. Live-сообщения после запуска обрабатываются отдельно.

---

## Источники

Базовый список хранится в `sources.json`.

Текущий snapshot содержит:

| Username | Priority | Category | Reliability |
|---|---:|---|---:|
| `rian_ru` | 10 | auto | 0.95 |
| `rt_russian` | 9 | auto | 0.92 |
| `readovkanews` | 9 | auto | 0.88 |
| `mash` | 9 | auto | 0.86 |
| `bazabazon` | 8 | auto | 0.84 |
| `ostorozhno_novosti` | 8 | auto | 0.85 |
| `shot_shot` | 8 | auto | 0.84 |
| `breakingmash` | 8 | auto | 0.82 |
| `rybar` | 10 | svo | 0.88 |
| `wargonzo` | 9 | svo | 0.82 |
| `meduzalive` | 6 | world | 0.80 |
| `infomoscow24` | 7 | russia | 0.84 |

Формат:

```json
{
  "sources": [
    {
      "username": "some_channel",
      "enabled": true,
      "priority": 8,
      "category": "politics",
      "reliability": 0.85
    }
  ]
}
```

Username указывается без `@`. Collector нормализует его и использует Telegram entity ID для live routing.

---

# Admin Addon v1.1.0

Admin Addon находится в `admin-addon/` и не является заменой основного News Bot. Это отдельный control plane, который подключается к существующему pipeline.

## Что даёт Addon

После установки можно управлять источниками из отдельного Telegram-бота:

```text
🛠 News Bot — управление

📊 Статистика     📈 По источникам
🟢 Статус         📰 Источники
➕ Добавить       🔄 Обновить
```

### Статистика

Раздел `📊 Статистика` показывает:

- полученные сообщения;
- количество событий;
- отклонённые события;
- опубликованные события;
- количество сообщений, связанных с событиями;
- периоды 24 часа и 7 дней.

### По источникам

Раздел `📈 По источникам` показывает активность каждого источника за последние 24 часа и сколько сообщений было связано с событиями.

### Источники

Для каждого источника доступны:

- статус включён/выключен;
- режим `primary` / `addon`;
- priority;
- category;
- reliability;
- статистика за 24 часа;
- последнее полученное сообщение.

### Добавление

Кнопка `➕ Добавить` переводит Admin Bot в режим ожидания username. Можно отправить, например:

```text
@new_source
```

Источник записывается в Registry как `owner=addon` и становится доступен Hot Worker без остановки основного сервиса.

### Enable / Disable

`⏸ Выключить` и `▶️ Включить` изменяют поле `enabled` в общем Registry.

Основной collector регулярно читает Registry, поэтому состояние источника применяется без ручного редактирования кода.

Кроме того, SQLite trigger блокирует вставку новых сообщений от отключённого источника. Это важно даже если старый/параллельный collector уже продолжает работать.

История при выключении не удаляется.

### Удаление

Для addon-источника операция удаления является soft-delete: источник отключается, а исторические сообщения сохраняются.

Для `primary`-источника удаление также не физически удаляет запись: он отключается, остаётся в Registry и сохраняет историю. Это защищает систему от повторного ingest через основной `sources.json`.

---

## Конфигурация основного News Bot

Создайте `.env` на сервере по `.env.example`.

Основные параметры:

```env
# Telegram
TG_API_ID=
TG_API_HASH=
TG_PHONE=
TG_SESSION=runtime/telegram/newsbot

# MAX
MAX_PHONE=
MAX_SESSION=max.db
MAX_WORK_DIR=runtime/max
MAX_CHANNEL_ID=

# LLM
GROQ_API_KEY=
GROQ_MODEL=qwen/qwen3-27b
MISTRAL_API_KEY=
MISTRAL_MODEL=mistral-small-latest
LLM_TIMEOUT=90

# Database / sources
DB_FILE=runtime/news.db
SOURCE_CONFIG=sources.json

# Processing
AUTO_PUBLISH=1
BACKFILL_LIMIT=100
RETENTION_DAYS=4
POLL_INTERVAL=5
DEDUP_HOURS=36
SIMILARITY_THRESHOLD=0.86
MIN_SOURCES_FOR_HIGH_CONFIDENCE=2

# Editorial
LANGUAGE=ru
NEWS_CATEGORIES=politics,svo,russia,world,economy,security
MAX_POST_LENGTH=3500

# Publishing thresholds
MIN_PRIORITY_PUBLISH=70
MIN_CONFIDENCE_PUBLISH=0.82

# LLM protection
LLM_MIN_INTERVAL_SECONDS=2
GROQ_COOLDOWN_SECONDS=30
```

Секреты, Telegram session DB, MAX session DB, runtime и `.venv` не должны попадать в Git.

---

## Конфигурация Admin Addon

Создайте:

```text
/opt/news-bot-v1/.env.admin
```

Минимальный пример:

```env
ADMIN_BOT_TOKEN=...
ADMIN_IDS=123456789
ADMIN_TG_SESSION=runtime/telegram/newsbot_admin

# Необязательно: отдельная MAX-сессия для Hot Worker
ADMIN_MAX_SESSION=runtime/max-admin/max-admin.db
ADMIN_MAX_WORK_DIR=runtime/max-admin
ADMIN_MAX_PHONE=+...

ADMIN_HOT_BACKFILL_LIMIT=5
ADMIN_HOT_POLL_SECONDS=5
```

`ADMIN_IDS` — список Telegram user ID через запятую. Только эти пользователи получают доступ к Admin Bot.

Addon сначала загружает основной `.env`, затем `.env.admin` с возможностью переопределения параметров.

---

## Установка Windows

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
```

Создайте конфигурацию:

```bat
copy .env.example .env
notepad .env
notepad sources.json
```

Первый запуск:

```bat
python main.py
```

Telethon может запросить код входа и пароль 2FA. Session сохраняется в `runtime/telegram/`.

---

## Установка Linux / production

Пример ручной установки:

```bash
cd /opt/news-bot-v1
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m py_compile main.py newsbot/*.py newsbot/core/*.py newsbot/telegram/*.py newsbot/llm/*.py newsbot/max/*.py
```

Запуск вручную:

```bash
cd /opt/news-bot-v1
.venv/bin/python main.py
```

Для production основной сервис используется как:

```text
news-bot.service
```

---

## Установка Admin Addon на сервере

```bash
cd /opt/news-bot-v1
.venv/bin/pip install -r admin-addon/requirements-addon.txt
```

Создайте `.env.admin`, затем можно проверить addon вручную:

```bash
cd /opt/news-bot-v1
source .venv/bin/activate
set -a
source .env.admin
set +a
PYTHONPATH=/opt/news-bot-v1/admin-addon:/opt/news-bot-v1 \
  python -m newsbot_admin.main
```

Для постоянной работы используется systemd unit из:

```text
admin-addon/systemd/newsbot-admin.service
```

Установка:

```bash
cp admin-addon/systemd/newsbot-admin.service /etc/systemd/system/newsbot-admin.service
systemctl daemon-reload
systemctl enable --now newsbot-admin
```

Основной `news-bot.service` при этом перезапускать не требуется.

---

## Проверка production

Основной сервис:

```bash
systemctl status news-bot --no-pager
journalctl -u news-bot -f --no-pager
```

Admin Addon:

```bash
systemctl status newsbot-admin --no-pager
journalctl -u newsbot-admin -f --no-pager
```

Полезные признаки нормальной работы в логах:

```text
Telegram LIVE monitor added source_entity ...
Telegram dynamic source added ...
telegram article queued ...
LLM request provider=groq ...
editor rejected ...
article ready ...
MAX publish START ...
MAX publish SUCCESS ...
Telegram source control changed ...
```

---

## SQLite

Основная БД:

```text
runtime/news.db
```

Ключевые таблицы:

```text
messages
  исходные Telegram-сообщения

events
  объединённые новостные события

event_messages
  связь сообщений с событиями

articles
  подготовленный редактором материал

managed_sources
  единый Registry источников

admin_stats
  служебная статистика addon
```

SQLite работает в WAL-режиме.

### Почему одна БД

Основной pipeline и Admin Addon должны видеть одно и то же состояние источников и событий. Поэтому Addon не создаёт отдельную копию Registry.

Одновременная работа процессов учитывается в коде: SQLite connection открывается с timeout/retry, а уникальность `(source, source_message_id)` защищает от повторного сохранения одного Telegram-сообщения.

---

## Безопасность и секреты

Не коммитьте:

```text
.env
.env.admin
runtime/
*.session
*.session-journal
*.db
.venv/
```

Особенно важно не публиковать:

- `TG_API_HASH`;
- `GROQ_API_KEY`;
- `MISTRAL_API_KEY`;
- `ADMIN_BOT_TOKEN`;
- Telegram session files;
- MAX session database.

В репозитории находится только `.env.example` без секретных значений.

---

## Структура проекта

```text
news-bot/
├── main.py
├── sources.json
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── smoke_test.py
├── setup.bat
├── newsbot/
│   ├── app.py
│   ├── config.py
│   ├── db.py
│   ├── logging_setup.py
│   ├── core/
│   │   └── dedup.py
│   ├── telegram/
│   │   └── collector.py
│   ├── llm/
│   │   └── adapter.py
│   └── max/
│       └── publisher.py
└── admin-addon/
    ├── VERSION
    ├── README.md
    ├── requirements-addon.txt
    ├── systemd/
    │   └── newsbot-admin.service
    └── newsbot_admin/
        ├── main.py
        ├── bot.py
        ├── config.py
        ├── control_db.py
        └── hot_worker.py
```

---

## Версия

Основной snapshot: **News Bot v1**.

Admin Addon: **v1.1.0**.

Production snapshot синхронизируется с серверной директорией `/opt/news-bot-v1`. Runtime-состояние и секреты намеренно остаются вне Git.

---

## Кратко: как работает система

```text
Telegram
   ↓
Collector / Hot Worker
   ↓
SQLite messages
   ↓
Dedup + event clustering
   ↓
Groq
   ↓ fallback
Mistral
   ↓
Editorial validation
   ↓
Priority + confidence
   ↓
PyMax
   ↓
MAX
```

Admin Addon работает сбоку:

```text
Admin Telegram Bot
        ↓
managed_sources
        ↓
Primary Collector + Hot Worker
        ↓
тот же news.db
```

Таким образом, основной News Bot отвечает за сбор, редактуру и публикацию, а Admin Addon — за оперативное управление источниками и наблюдение за pipeline.