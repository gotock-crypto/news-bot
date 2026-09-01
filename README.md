# News Bot

AI-powered news monitoring and cross-platform publishing pipeline.

Система автоматически получает новые публикации из Telegram-источников, анализирует их с помощью LLM, определяет категорию, приоритет и связь с уже известными событиями, после чего публикует подходящие материалы в MAX.

Проект работает в production на Linux/VPS и рассчитан на непрерывную автоматическую работу.

---

## Содержание

- [О проекте](#о-проекте)
- [Основной pipeline](#основной-pipeline)
- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Telegram Collector](#telegram-collector)
- [LIVE watermark](#live-watermark)
- [Dynamic Source Control](#dynamic-source-control)
- [Source Registry](#source-registry)
- [AI / LLM](#ai--llm)
- [Event Resolver](#event-resolver)
- [Editorial Decision](#editorial-decision)
- [MAX Publisher](#max-publisher)
- [Threads](#threads)
- [Admin Bot](#admin-bot)
- [SQLite и runtime state](#sqlite-и-runtime-state)
- [Конфигурация](#конфигурация)
- [Production запуск](#production-запуск)
- [Диагностика](#диагностика)
- [Git и GitHub](#git-и-github)
- [Безопасность](#безопасность)
- [Текущий production status](#текущий-production-status)
- [Ограничения и следующие этапы](#ограничения-и-следующие-этапы)

---

# О проекте

News Bot — автоматизированная система мониторинга новостей и публикации подготовленных материалов.

Основная задача:

```text
Telegram-источники
        ↓
сбор новых сообщений
        ↓
дедупликация
        ↓
LLM-анализ
        ↓
определение события
        ↓
редакторская логика
        ↓
публикация
        ↓
MAX
```

Система ориентирована прежде всего на оперативные новости.

Основные категории определяются конфигурацией и LLM и могут включать:

- политику;
- Россию;
- мир;
- экономику;
- безопасность;
- военную тематику;
- другие категории.

---

# Основной pipeline

Полная логическая схема:

```text
                         ┌─────────────────────┐
                         │ Telegram channels   │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Telegram Collector  │
                         │                     │
                         │ LIVE + polling      │
                         │ watermark           │
                         │ source isolation    │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │       SQLite        │
                         │ state / history     │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │     LLM analysis    │
                         │                     │
                         │ category            │
                         │ priority            │
                         │ confidence          │
                         │ publish decision    │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │   Event Resolver    │
                         │                     │
                         │ NEW / DUPLICATE     │
                         │ UPDATE              │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Editorial Decision  │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │    MAX Publisher    │
                         └──────────┬──────────┘
                                    │
                                    ▼
                              MAX channel
```

Дополнительный control plane:

```text
                    ┌───────────────────┐
                    │     Admin Bot     │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ Source Registry   │
                    │      SQLite       │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ Telegram Collector│
                    └───────────────────┘
```

---

# Возможности

## Telegram monitoring

- несколько Telegram-источников;
- Telethon/MTProto;
- LIVE updates;
- регулярный polling как safety-net;
- отдельный watermark для каждого источника;
- изоляция ошибок отдельных источников;
- FloodWait/cooldown;
- хранение состояния в SQLite;
- динамическое управление источниками.

## AI processing

LLM используется для:

- классификации материала;
- определения категории;
- определения приоритета;
- оценки confidence;
- решения о публикации;
- анализа связи материала с существующими событиями;
- определения NEW / DUPLICATE / UPDATE;
- оценки существенности обновления.

LLM не является единственным уровнем принятия решения: результат проходит через программную и редакторскую логику pipeline.

## Дедупликация

Система не рассматривает каждое Telegram-сообщение как независимую новость.

После получения материала выполняется поиск связанных существующих событий.

Возможные результаты:

```text
NEW
DUPLICATE
UPDATE
```

Это позволяет не публиковать одну и ту же историю многократно только потому, что её одновременно сообщили разные источники.

---

# Архитектура

Основные части проекта:

```text
newsbot/
│
├── app.py
├── config.py
├── db.py
├── admin_bot.py
│
├── core/
│   └── pipeline.py
│
├── telegram/
│   └── collector.py
│
└── threads/
    ├── __init__.py
    └── publisher.py
```

Ответственность компонентов:

```text
app.py
    Сборка и запуск приложения.

config.py
    Конфигурация.

db.py
    SQLite и persistent state.

admin_bot.py
    Управление системой и источниками.

core/pipeline.py
    Основная редакторская обработка.

telegram/collector.py
    Получение сообщений Telegram.

threads/publisher.py
    Threads publishing layer.
```

---

# Telegram Collector

Collector отвечает за получение сообщений из Telegram.

Используются два механизма:

```text
LIVE updates
       +
polling safety-net
```

LIVE updates обеспечивают оперативное получение сообщений.

Polling периодически проверяет источники и дополнительно защищает от пропусков в event stream.

Обработка источников изолирована, поэтому ошибка отдельного канала не должна останавливать весь collector.

---

# LIVE watermark

Для каждого источника хранится последний обработанный Telegram message ID:

```text
last_message_id
```

Логика:

```text
message_id <= watermark
        ↓
      старое

message_id > watermark
        ↓
      новое
```

Watermark сохраняется в SQLite.

Это предотвращает повторную обработку старой истории после рестарта.

---

# Новый источник

Новый источник не реплеит историю.

Первичная инициализация:

```text
/add_source @example
        ↓
source registration
        ↓
первый polling
        ↓
latest message ID = watermark
        ↓
последнее сообщение пропускается
        ↓
LIVE monitoring
```

После этого обрабатываются только сообщения новее watermark.

Если канал пока не содержит сообщений, watermark остаётся `0`, и инициализация будет повторена на следующей проверке.

Это особенно важно для крупных каналов: добавление источника не создаёт массовую очередь старой истории.

---

# Dynamic Source Control

Источниками можно управлять во время работы системы через Admin Bot.

Изменения записываются непосредственно в БД и подхватываются collector на следующем polling cycle.

Перезапуск `news-bot.service` для обычного изменения источников не требуется.

Поддерживаются команды:

```text
/add_source @channel
/enable_source @channel
/disable_source @channel
/delete_source @channel
/sources
```

## /add_source

```text
/add_source @example
```

Создаёт или включает addon-источник.

Для нового источника первый poll устанавливает watermark на последнее существующее сообщение и не отправляет его в pipeline.

## /enable_source

```text
/enable_source @example
```

Включает источник.

Существующий watermark сохраняется.

## /disable_source

```text
/disable_source @example
```

Временно выключает источник.

Watermark сохраняется.

## /delete_source

```text
/delete_source @example
```

Удаляет addon-источник из registry.

## /sources

```text
/sources
```

Показывает зарегистрированные источники и их состояние.

---

# Source Registry

Источники хранятся в таблице `sources`.

Основные поля:

```text
username
enabled
priority
category
reliability
owner
telegram_entity_id
title
created_at
updated_at
last_message_id
```

Тип источника определяется полем `owner`:

```text
primary
addon
```

`primary` — штатный источник проекта.

`addon` — динамически добавленный источник.

---

# AI / LLM

LLM является отдельным уровнем pipeline и используется для анализа поступивших материалов.

В production логах фиксируются provider, model, category, priority, confidence и итоговые решения.

Модель не должна рассматриваться как единственный источник истины. Архитектура использует fallback между доступными providers и отдельную логику event resolution.

Типовой flow:

```text
material
   ↓
LLM analysis
   ↓
structured result
   ↓
validation / business rules
   ↓
Event Resolver
   ↓
Editorial Decision
```

---

# LLM fallback

Внешние AI providers могут возвращать:

- HTTP 429;
- HTTP 5xx;
- timeout;
- сетевую ошибку;
- временную недоступность;
- некорректный ответ.

В production pipeline используется fallback на следующий доступный provider.

Например:

```text
Provider A
    │
    ├── success ──► result
    │
    └── 429/error
           │
           ▼
       Provider B
           │
           ▼
         result
```

Это не устраняет rate limits, но не позволяет единичному сбою одного provider автоматически остановить весь pipeline.

---

# Event Resolver

Event Resolver определяет отношение нового материала к уже известным событиям.

Основные результаты:

```text
NEW
DUPLICATE
UPDATE
```

## NEW

Материал относится к новому событию.

```text
new message
     ↓
нет подходящего существующего event
     ↓
NEW
```

## DUPLICATE

Материал относится к уже известному событию и не содержит достаточного количества новой информации.

```text
new message
     ↓
existing event
     ↓
нет существенных новых фактов
     ↓
DUPLICATE
```

## UPDATE

Материал относится к существующему событию, но содержит существенную новую информацию.

```text
new message
     ↓
existing event
     ↓
новые существенные факты
     ↓
UPDATE
```

Для UPDATE применяется дополнительная оценка важности нового материала.

---

# Независимые источники

Система может учитывать количество независимых источников, сообщающих об одном событии.

При этом Telegram entity ID важен для исключения ложного ощущения независимого подтверждения, когда разные alias относятся к одной и той же сущности.

---

# Editorial Decision

После AI analysis и event resolution принимается итоговое редакторское решение.

```text
material
   ↓
category
   ↓
priority
   ↓
confidence
   ↓
event resolution
   ↓
editorial rules
   ↓
publish / skip
```

Таким образом, решение о публикации не сводится к одному ответу LLM.

---

# MAX Publisher

MAX является основным каналом публикации текущего production pipeline.

Поток:

```text
Telegram
    ↓
Collector
    ↓
LLM
    ↓
Event Resolver
    ↓
Editorial Decision
    ↓
MAX Publisher
    ↓
MAX channel
```

В production система уже выполняет реальные публикации в MAX.

---

# Threads

В проект интегрирован отдельный Threads publishing layer:

```text
newsbot/threads/
├── __init__.py
└── publisher.py
```

В `.env.example` предусмотрены:

```env
THREADS_ENABLED=0
THREADS_DRY_RUN=1
THREADS_ACCESS_TOKEN=
THREADS_API_BASE_URL=https://graph.threads.net
THREADS_MAX_LENGTH=480
```

По умолчанию production publishing в Threads отключён.

Рекомендуемый порядок включения:

```text
THREADS_ENABLED=0
THREADS_DRY_RUN=1
        ↓
проверка OAuth/API
        ↓
проверка dry-run
        ↓
проверка логов
        ↓
THREADS_ENABLED=1
```

---

# Admin Bot

Admin Bot является control plane проекта.

Основные команды:

```text
/start
/menu
/status
/stats
/telegram
/sources

/add_source @channel
/enable_source @channel
/disable_source @channel
/delete_source @channel
```

Команды управления источниками доступны авторизованным администраторам.

---

# SQLite и runtime state

Основная production база:

```text
runtime/news.db
```

Runtime хранит состояние системы, включая:

- source registry;
- watermark;
- историю обработки;
- события;
- дедупликацию;
- публикации;
- служебное состояние интеграций.

Runtime state не является исходным кодом и не должен переноситься между production и Git без явной необходимости.

---

# Конфигурация

Шаблон безопасной конфигурации:

```text
.env.example
```

Production secrets находятся вне Git.

Основные группы настроек:

```text
Telegram
LLM
Event Resolver
Editorial logic
MAX
Threads
Database
Polling
Source control
```

---

# Что НЕ должно попадать в Git

Никогда не коммитить:

```text
.env
.env.admin
runtime/
*.db
*.session
*.session-journal
.venv/
__pycache__/
*.pyc
```

Также нельзя помещать в репозиторий:

```text
Telegram API credentials
Telegram sessions
LLM API keys
MAX credentials
Threads access tokens
Admin Bot tokens
```

---

# Production запуск

Основной production directory:

```text
/opt/news-bot-v1
```

Virtual environment:

```text
/opt/news-bot-v1/.venv
```

Entrypoint:

```text
/opt/news-bot-v1/main.py
```

Основной systemd service:

```text
news-bot.service
```

---

# Systemd

Проверка сервиса:

```bash
systemctl status news-bot.service --no-pager -l
```

Проверка состояния:

```bash
systemctl is-active news-bot.service
```

Перезапуск:

```bash
systemctl restart news-bot.service
```

После перезапуска:

```bash
systemctl status news-bot.service --no-pager -l
```

---

# Production logs

Последние строки:

```bash
journalctl -u news-bot.service -n 100 --no-pager
```

Realtime:

```bash
journalctl -u news-bot.service -f
```

Polling:

```bash
journalctl -u news-bot.service -f | grep -E 'poll cycle|poll source'
```

LLM:

```bash
journalctl -u news-bot.service -f | grep -E 'LLM|llm'
```

MAX:

```bash
journalctl -u news-bot.service -f | grep -E 'MAX|max|publish'
```

---

# Быстрая диагностика

## Проверка Python syntax

```bash
cd /opt/news-bot-v1

.venv/bin/python -m py_compile \
  newsbot/admin_bot.py \
  newsbot/app.py \
  newsbot/config.py \
  newsbot/db.py \
  newsbot/core/pipeline.py \
  newsbot/telegram/collector.py \
  newsbot/threads/publisher.py
```

## Проверка источников

```bash
cd /opt/news-bot-v1

.venv/bin/python - <<'PY'
import sqlite3

con = sqlite3.connect("runtime/news.db")
con.row_factory = sqlite3.Row

try:
    rows = con.execute("""
        SELECT username, enabled, owner, last_message_id
        FROM sources
        ORDER BY username
    """).fetchall()

    for row in rows:
        print(dict(row))
finally:
    con.close()
PY
```

## Проверка Git

```bash
cd /opt/news-bot-v1
git status
git branch --show-current
git log -1 --oneline
git remote -v
```

---

# Git и GitHub

Основной репозиторий:

```text
https://github.com/gotock-crypto/news-bot
```

Основная ветка:

```text
main
```

Remote:

```text
origin
```

Production source tree синхронизируется с `origin/main`.

---

# Git workflow

Перед commit:

```bash
git status
git diff --stat
git diff
```

Добавление изменений:

```bash
git add .
```

Commit:

```bash
git commit -m "описание изменения"
```

Push:

```bash
git push origin main
```

Проверка синхронизации:

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

SHA локального `HEAD` и `origin/main` должны совпадать.

---

# SSH для GitHub

Production server использует отдельный SSH key для GitHub.

Типовая конфигурация:

```text
/root/.ssh/id_ed25519_github
```

SSH config:

```text
/root/.ssh/config
```

Пример:

```sshconfig
Host github.com
    HostName github.com
    User git
    IdentityFile /root/.ssh/id_ed25519_github
    IdentitiesOnly yes
```

Проверка:

```bash
ssh -T git@github.com
```

---

# Безопасность

Production credentials должны находиться вне Git repository.

Особенно чувствительны:

```text
Telegram API ID / hash
Telegram sessions
LLM API keys
MAX credentials
Threads access token
Admin Bot token
```

SSH private keys также никогда не должны передаваться в чат или коммититься в Git.

---

# Rate limits и внешние ошибки

Внешние сервисы могут ограничивать частоту запросов.

Возможны:

```text
HTTP 429
HTTP 5xx
FloodWait
timeout
connection error
```

Система должна реагировать на такие ситуации контролируемо:

- изолировать проблемный источник;
- использовать cooldown;
- использовать retry там, где это безопасно;
- использовать LLM fallback;
- не останавливать весь collector из-за одной ошибки.

---

# Что считается нормальным поведением

Нормально:

```text
poll source=<name> new=0
```

Это означает, что новых сообщений не обнаружено.

Нормально:

```text
startup_backfill
```

если система пропускает сообщения, относящиеся к стартовой истории.

Нормально:

```text
cooldown
```

для временно проблемного внешнего сервиса.

Ненормально:

```text
collector полностью остановился
```

или:

```text
ошибка одного источника остановила обработку остальных источников
```

---

# Текущий production status

Система находится за пределами стадии первоначального прототипа.

Основная рабочая цепочка:

```text
Telegram
    ↓
LIVE collector
    ↓
SQLite
    ↓
LLM analysis
    ↓
Event Resolver
    ↓
Editorial Decision
    ↓
MAX Publisher
    ↓
MAX channel
```

Текущий уровень включает:

```text
✓ Telegram monitoring
✓ LIVE updates
✓ polling safety-net
✓ per-source watermark
✓ startup backfill protection
✓ dynamic source registry
✓ add source
✓ enable source
✓ disable source
✓ delete addon source
✓ SQLite state
✓ deduplication
✓ event resolution
✓ NEW / DUPLICATE / UPDATE
✓ independent source handling
✓ LLM classification
✓ priority
✓ confidence
✓ editorial decision
✓ LLM fallback
✓ MAX publishing
✓ Admin Bot
✓ dynamic runtime control
✓ systemd production runtime
✓ GitHub source control
✓ Threads publisher layer
```

Реальный production pipeline уже обрабатывает сообщения Telegram и выполняет публикации в MAX.

---

# Текущие ограничения

Система рабочая, но качество продукта ещё можно существенно улучшать.

Основные направления:

- качество заголовков;
- качество краткого текста;
- качество категорий;
- качество priority;
- качество confidence;
- качество NEW/DUPLICATE/UPDATE;
- снижение false positive;
- снижение false negative;
- скорость от появления новости до публикации;
- устойчивость к LLM rate limits;
- расширение набора независимых LLM providers;
- полноценный production Threads publishing;
- расширение observability;
- метрики качества;
- контроль редакторских ошибок;
- улучшение ranking источников;
- улучшение event clustering.

---

# Следующий уровень развития

Инфраструктурная часть уже позволяет развивать систему без переделки базовой архитектуры.

Приоритетный следующий этап — повышение качества editorial layer:

1. улучшение определения существенности новости;
2. улучшение заголовков;
3. улучшение summary;
4. более точная работа с UPDATE;
5. более точная работа с DUPLICATE;
6. улучшение event clustering;
7. оценка качества источников;
8. расширение AI fallback;
9. улучшение latency;
10. полноценный multi-platform publishing.

---

# Главный принцип архитектуры

Проект не пытается решить задачу за счёт одной «умной» нейросети.

Надёжность строится на нескольких независимых уровнях:

```text
                    deterministic code
                            +
                       database state
                            +
                         watermarks
                            +
                      source isolation
                            +
                       deduplication
                            +
                       event resolver
                            +
                            LLM
                            +
                          fallback
                            +
                     editorial rules
                            +
                          logging
```

Ключевой принцип:

```text
ошибка одной модели
        ≠
ошибка всей новостной системы
```

---

# License

Internal / project-specific software.

Copyright © gotock-crypto.
