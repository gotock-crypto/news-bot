# News Bot

Автоматический новостной pipeline:

Telegram → ingestion → deduplication → event resolution → LLM editorial → publication → MAX

Проект предназначен для автоматического сбора сообщений из Telegram-источников, определения новостных событий, устранения дублей, редакторской обработки через LLM и публикации готового материала в MAX.

---

## 1. Что это за проект

News Bot начинался как простой скрипт:

Telegram → MAX

В процессе разработки архитектура была существенно расширена и превратилась в полноценный новостной pipeline:

                    ┌──────────────────┐
                    │ Telegram sources │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Telegram         │
                    │ Collector        │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ SQLite           │
                    │ messages         │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Deduplication    │
                    │                  │
                    │ exact hash       │
                    │ lexical match    │
                    │ event resolver   │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ News Event       │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ LLM Editorial    │
                    │ Engine           │
                    └────────┬─────────┘
                             │
                     publish / reject
                             │
                             ▼
                    ┌──────────────────┐
                    │ MAX Publisher    │
                    └──────────────────┘

Основная задача системы — не просто переписать Telegram-сообщение, а решить:

1. Это вообще новость?
2. Это новая новость или уже известное событие?
3. Если это известное событие — это повтор или существенное обновление?
4. Можно ли материал публиковать?
5. Как сформулировать его без выдуманных фактов?
6. Нужно ли отправлять его в MAX?

---

# 2. Основные возможности

На текущем этапе реализованы следующие основные компоненты:

- Telegram MTProto collector;
- работа с несколькими Telegram-источниками;
- LIVE получение новых сообщений;
- периодический polling как safety-net;
- per-source watermark;
- обработка Telegram FloodWait;
- SQLite storage;
- exact duplicate detection;
- lexical similarity;
- semantic/event resolution;
- объединение сообщений разных источников в один event;
- LLM editorial layer;
- Groq как основной LLM provider;
- Mistral как fallback;
- категории новостей;
- priority;
- reliability;
- confidence;
- автоматическое решение publish / reject;
- публикация в MAX;
- публикация UPDATE как отдельного сообщения;
- связь UPDATE с предыдущей публикацией;
- reply на предыдущий MAX-пост;
- systemd production service;
- тесты pipeline;
- резервные копии файлов перед изменениями;
- production logging.

---

# 3. Структура проекта

Текущая структура репозитория:

```text
news-bot/
├── .env.example
├── .gitignore
├── README.md
├── deploy.sh
├── main.py
├── news-bot.service
├── requirements.txt
├── requirements-dev.txt
├── sources.json
│
├── newsbot/
│   ├── __init__.py
│   ├── admin_bot.py
│   ├── app.py
│   ├── config.py
│   ├── db.py
│   ├── logging_setup.py
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── dedup.py
│   │   └── event_resolver.py
│   │
│   ├── llm/
│   │   ├── __init__.py
│   │   └── adapter.py
│   │
│   ├── max/
│   │   ├── __init__.py
│   │   └── publisher.py
│   │
│   └── telegram/
│       ├── __init__.py
│       └── collector.py
│
└── tests/
    └── test_core.py
```

---

# 4. Назначение основных файлов

## main.py

Главная точка запуска приложения.

Запускает основной News Bot.

Production:

```text
/opt/news-bot-v1/main.py
```

---

## newsbot/app.py

Связывает основные компоненты приложения.

Логически здесь находится orchestration:

```text
settings
    ↓
database
    ↓
LLM
    ↓
Telegram collector
    ↓
pipeline
    ↓
MAX publisher
```

---

## newsbot/config.py

Загрузка конфигурации.

Основные параметры приходят из environment variables.

Секреты не должны храниться в Git.

Используется:

```text
.env
```

а в репозитории находится только:

```text
.env.example
```

---

## newsbot/db.py

SQLite persistence layer.

SQLite используется для хранения состояния новостного pipeline.

Основные сущности:

```text
messages
events
event_messages
articles
publications
managed_sources
```

Также используются системные/служебные таблицы.

---

# 5. SQLite

Production database:

```text
runtime/news.db
```

Runtime database не должна попадать в Git.

Причина:

- база содержит состояние приложения;
- в ней находятся обработанные сообщения;
- есть текущие watermark;
- есть история событий;
- есть информация о публикациях;
- структура runtime может мигрироваться независимо от исходного кода.

---

## Основные таблицы

### messages

Сырые сообщения Telegram.

Логически:

```text
Telegram message
        ↓
messages
```

---

### events

Новостные события.

Несколько Telegram-сообщений могут относиться к одному событию:

```text
message A ─┐
message B ─┼──> event
message C ─┘
```

---

### event_messages

Связь:

```text
event ↔ message
```

Позволяет хранить несколько источников одного события.

---

### articles

Редакционные материалы.

Это уже не просто Telegram message, а материал, прошедший editorial processing.

Типы материала:

```text
NEW
UPGRADE
```

NEW — первая публикация события.

UPGRADE — отдельная публикация с существенным новым развитием уже опубликованного события.

---

### publications

Факт публикации материала.

Здесь хранится:

```text
event_id
article_id
kind
max_message_id
parent_publication_id
created_at
```

parent_publication_id позволяет построить цепочку:

```text
NEW
 ↓
UPGRADE
 ↓
UPGRADE
 ↓
UPGRADE
```

---

# 6. Telegram Collector

Файл:

```text
newsbot/telegram/collector.py
```

Telegram используется как источник новостей.

Collector работает с Telegram через пользовательскую MTProto-сессию.

Это позволяет читать сообщения из каналов, доступных аккаунту.

---

## LIVE processing

Основной путь:

```text
Telegram NewMessage
        ↓
collector
        ↓
pipeline
```

---

## Polling safety-net

Помимо realtime events используется периодический polling.

Причина — realtime event может быть потерян.

Поэтому система периодически проверяет историю источников.

В production это выглядит примерно так:

```text
poll cycle
    ↓
source 1
source 2
source 3
...
source N
```

---

## Watermark

Для каждого источника хранится последний обработанный message ID.

Принцип:

```text
source
  ↓
last_message_id
  ↓
получаем только новые сообщения
```

Это позволяет не перечитывать всю историю при каждом цикле.

---

## FloodWait

Telegram может ограничивать слишком частые запросы History API.

Поэтому FloodWait должен обрабатываться изолированно.

Ошибка одного источника не должна останавливать остальные источники.

---

# 7. Sources

Начальный registry находится в:

```text
sources.json
```

Источник имеет параметры вроде:

```text
username
category
priority
reliability
enabled
```

---

## Priority

Priority отвечает за редакторскую важность источника.

Например:

```text
rian_ru      10
rybar        10
rt_russian    9
mash          9
wargonzo      9
```

---

## Reliability

Reliability — доверие к источнику.

Это не то же самое, что priority.

priority отвечает:

> Насколько важен этот источник?

reliability отвечает:

> Насколько мы доверяем информации из этого источника?

Это разные параметры.

---

# 8. Deduplication

Одна из самых важных частей системы.

Без deduplication ситуация выглядела бы так:

```text
РИА → новость
ТАСС → та же новость
RT → та же новость
Mash → та же новость
Baza → та же новость
```

И бот мог бы отправить пять публикаций одного события.

Цель:

```text
5 сообщений
    ↓
1 event
    ↓
1 публикация
```

---

# 9. Exact duplicate

Первый уровень:

```text
text
 ↓
normalize
 ↓
hash
```

Если два сообщения практически идентичны, система может обнаружить это напрямую.

---

# 10. Lexical similarity

Второй уровень — сравнение текстов.

Например:

```text
Источник A:
В Москве произошёл пожар...

Источник B:
В столице произошёл пожар...
```

Тексты не идентичны, но могут описывать одно событие.

Для этого используется:

```text
newsbot/core/dedup.py
```

---

# 11. Semantic Event Resolver

Файл:

```text
newsbot/core/event_resolver.py
```

Это следующий уровень защиты от дублей.

Resolver определяет:

```text
duplicate
update
new
```

Это особенно важно для сообщений с разными формулировками.

---

# 12. Pipeline

Основная бизнес-логика находится в:

```text
newsbot/core/pipeline.py
```

Полный путь:

```text
Telegram
   ↓
message
   ↓
exact dedup
   ↓
lexical dedup
   ↓
LLM editorial
   ↓
event resolver
   ↓
new / duplicate / update
   ↓
article
   ↓
publication
   ↓
MAX
```

---

# 13. Обработка нового события

Для нового события pipeline:

```text
1. Получает сообщение
2. Проверяет exact duplicate
3. Проверяет lexical duplicate
4. Отправляет кандидата в LLM
5. Получает editorial decision
6. Создаёт event
7. Если publish=false — событие отклоняется
8. Если publish=true — создаётся article
9. Article публикуется в MAX
10. Publication записывается в DB
```

---

# 14. Editorial LLM

LLM слой находится в:

```text
newsbot/llm/adapter.py
```

LLM используется не только для генерации текста.

Он выполняет роль редактора.

Модель должна определить:

- полноценная ли это новость;
- что является фактом;
- насколько материал важен;
- к какой категории он относится;
- можно ли публиковать;
- какой priority;
- какой confidence;
- как сформировать заголовок;
- как написать текст без выдуманных деталей.

---

# 15. LLM providers

Архитектура:

```text
Groq
  ↓
primary
  ↓
ошибка / недоступность
  ↓
Mistral
```

Основной production provider:

```text
Groq
```

В production использовалась модель:

```text
qwen/qwen3.8-27b
```

Mistral предусмотрен как fallback.

---

# 16. LLM rate limiting

В adapter присутствует общий lock и минимальный интервал между запросами.

Это необходимо для:

- защиты от rate limits;
- уменьшения количества ошибок API;
- контролируемой нагрузки;
- последовательного использования нескольких LLM requests.

---

# 17. Editorial decision

Пример результата:

```text
category=other
priority=0
confidence=0.90
publish=False
```

Это означает, что модель обработала сообщение, но редакторское решение — не публиковать.

Таким образом:

```text
LLM ≠ просто генератор
```

LLM является частью editorial engine.

---

# 18. Официальные заявления

Отдельное редакционное правило:

одно официальное заявление не обязано автоматически отклоняться только потому, что нет второго независимого источника.

При публикации должна сохраняться атрибуция:

```text
По данным ...
Как сообщили ...
По заявлению ...
Об этом сообщили в ...
```

Нельзя превращать заявление источника в установленный системой факт.

---

# 19. Categories

В pipeline используются категории.

В коде поддерживаются:

```text
politics
svo
russia
world
economy
security
other
```

Категория влияет на editorial decision.

---

# 20. Confidence

LLM возвращает confidence.

Например:

```json
{
  "confidence": 0.95
}
```

Confidence используется как дополнительная защита перед публикацией.

В production существуют настройки вида:

```text
MIN_CONFIDENCE_PUBLISH
```

---

# 21. Automatic publishing

Основной флаг:

```text
AUTO_PUBLISH
```

Безопасный режим:

```text
AUTO_PUBLISH=0
```

В этом режиме материал может пройти весь pipeline, но автоматически не отправляется в MAX.

Production:

```text
AUTO_PUBLISH=1
```

Публикация разрешена после прохождения:

```text
dedup
 ↓
event resolution
 ↓
editorial decision
 ↓
confidence/priority checks
 ↓
MAX
```

---

# 22. MAX Publisher

Файл:

```text
newsbot/max/publisher.py
```

MAX является конечной точкой публикации.

Логика:

```text
article
   ↓
MAX Publisher
   ↓
MAX chat/channel
```

В production publisher подключается к MAX через PyMax.

---

# 23. NEW publication

Для нового события создаётся первая публикация.

Схема:

```text
event
 ↓
article NEW
 ↓
MAX
 ↓
publication NEW
```

В publications сохраняется MAX message ID.

---

# 24. UPDATE / UPGRADE

Для уже опубликованного события используется отдельный механизм.

Если появляется новый источник по тому же event:

```text
existing_event()
```

сначала находится последняя опубликованная версия:

```text
last_publication()
```

Затем выполняется:

```text
classify_update()
```

Если новое сообщение не содержит существенной новой информации:

```text
important=false
```

и публикации нет.

Если информация существенная:

```text
important=true
```

затем вызывается:

```text
compose_event_update()
```

После этого создаётся:

```text
article kind=UPGRADE
```

и отдельная MAX publication.

---

# 25. UPDATE не редактирует старую публикацию

Это принципиально важно.

Старая публикация:

```text
NEW
```

остаётся в MAX без изменений.

Новое развитие:

```text
UPGRADE
```

выходит отдельным сообщением.

Связь:

```text
NEW publication
       ↓
UPGRADE publication
```

В MAX UPDATE отправляется как reply на предыдущую публикацию:

```text
reply_to=previous_max_message_id
```

---

# 26. UPDATE classifier

Метод:

```text
classify_update()
```

должен отличать:

### Не UPDATE

- перефразировку;
- повтор старых фактов;
- более подробное описание;
- второстепенную деталь;
- повторное заявление другого источника;
- обычную хронологию;
- информацию, которая логично следует из уже опубликованного.

### UPDATE

Только существенное развитие:

- изменение числа погибших/пострадавших;
- существенное изменение масштаба;
- новое важное действие;
- новое последствие;
- изменение статуса;
- официальное подтверждение или опровержение ключевого факта;
- информация, заметно меняющая понимание события.

Используется score:

```text
0–30     обычный повтор
31–69    новая информация, но недостаточная
70–89    существенный UPDATE
90–100   критически важное изменение
```

important=true допускается только при:

```text
score >= 70
```

---

# 27. UPDATE composer

Метод:

```text
compose_event_update()
```

должен создать новый самостоятельный пост.

Главные правила:

1. Не редактировать старый пост.
2. Писать только новые существенные факты.
3. Не повторять старый текст.
4. Не выдумывать.
5. Сохранять атрибуцию.
6. Заголовок должен описывать именно новое развитие.
7. Текст должен быть коротким.
8. При отсутствии новых существенных фактов:

```text
publish=false
```

---

# 28. Важный результат тестирования UPDATE

В ходе разработки проводились отдельные тесты на существующих events.

Тесты показали, что добавление новых конкретных обстоятельств может корректно определяться как UPDATE:

```text
important=true
score=75
```

Однако при повторном похожем запросе модель могла принять противоположное решение:

```text
important=false
score=45
```

Причина — вероятностная природа LLM.

Это важный результат разработки:

LLM не является полностью детерминированным классификатором.

---

# 29. Главный вывод по UPDATE

LLM должен помогать принимать решение, но критические правила должны быть защищены кодом.

Правильная архитектура:

```text
LLM
 ↓
candidate score
 ↓
deterministic validation
 ↓
publish / reject
```

а не:

```text
LLM
 ↓
безусловная публикация
```

Особенно это важно для:

- числа погибших;
- числа пострадавших;
- задержанных;
- изменения статуса;
- официального подтверждения;
- нового действия;
- существенного последствия.

---

# 30. Административное управление источниками

В архитектуре проекта предусмотрен control-plane для управления источниками.

Концепция:

```text
sources.json
      ↓
initial registry
      ↓
runtime DB
      ↓
managed_sources
      ↓
collector
```

Источник получает:

```text
username
enabled
priority
category
reliability
owner
telegram_entity_id
title
last_message_id
```

---

# 31. managed_sources

managed_sources — runtime registry источников.

Это позволяет отделить:

```text
исходную конфигурацию
```

от:

```text
текущего production состояния
```

Источник может быть выключен без изменения Git-файла.

---

# 32. Dynamic source control

Архитектурно предусмотрено управление:

```text
enabled
priority
category
reliability
```

без изменения исходного sources.json.

Идея:

```text
Admin control
      ↓
managed_sources
      ↓
collector
```

Это позволяет включать/выключать источники без изменения кода.

---

# 33. Admin Bot

В архитектуре проекта также присутствует административный контур.

Его задача — управление системой, а не публикация новостей.

Концептуально:

```text
Telegram Admin Bot
        ↓
Control DB
        ↓
managed_sources
        ↓
Collector
```

Важно: текущий Git snapshot не содержит отдельной директории admin-addon; поэтому этот раздел описывает реализованный/проверенный архитектурный контур, а не утверждает наличие отдельного addon-каталога в текущем репозитории.

---

# 34. Production

Production сервер:

```text
/opt/news-bot-v1
```

Основной service:

```text
news-bot.service
```

Запуск:

```bash
systemctl start news-bot.service
```

Остановка:

```bash
systemctl stop news-bot.service
```

Перезапуск:

```bash
systemctl restart news-bot.service
```

Статус:

```bash
systemctl status news-bot.service --no-pager -l
```

---

# 35. Production logs

Онлайн просмотр:

```bash
journalctl -u news-bot.service -f -o cat
```

Последние 300 записей:

```bash
journalctl -u news-bot.service -n 300 --no-pager -o cat
```

Фильтрация pipeline:

```bash
journalctl -u news-bot.service -n 300 --no-pager -o cat \
  | grep -E "UPDATE|UPGRADE|DUPLICATE|NEW event|LLM|error|failed"
```

---

# 36. Production restart workflow

После изменения Python:

```bash
cd /opt/news-bot-v1

./.venv/bin/python -m py_compile \
  newsbot/db.py \
  newsbot/llm/adapter.py \
  newsbot/core/pipeline.py \
  newsbot/telegram/collector.py

systemctl restart news-bot.service

systemctl status news-bot.service --no-pager -l
```

После этого:

```bash
journalctl -u news-bot.service -n 100 --no-pager -o cat
```

---

# 37. Virtual environment

Production Python environment:

```text
/opt/news-bot-v1/.venv/
```

Запуск Python:

```bash
./.venv/bin/python
```

Установка зависимостей:

```bash
./.venv/bin/pip install -r requirements.txt
```

Development dependencies:

```bash
./.venv/bin/pip install -r requirements-dev.txt
```

---

# 38. Configuration

Секреты должны храниться только на сервере.

Используется:

```text
.env
```

Пример:

```text
.env.example
```

Нельзя коммитить:

```text
.env
.env.admin
Telegram session
MAX session
runtime/
```

---

# 39. Git

В репозитории хранится исходный код.

Не хранится production state.

В .gitignore исключены:

```text
__pycache__/
*.py[cod]
.env
runtime/
*.session
*.session-journal
```

Git должен хранить:

```text
code
config templates
tests
deployment files
documentation
```

но не:

```text
database
sessions
secrets
runtime state
```

---

# 40. Production snapshot

29.08.2026 был создан production snapshot проекта.

После очистки Python cache он был закоммичен:

```text
82c5eea Clean production snapshot
```

После этого snapshot был отправлен в GitHub:

```text
https://github.com/gotock-crypto/news-bot
```

ветка:

```text
main
```

Snapshot был установлен как текущий origin/main.

---

# 41. Ошибка №1 — неправильное имя systemd service

На одном из этапов использовалась команда:

```bash
systemctl restart news-bot-v1
```

которая дала:

```text
Unit news-bot-v1.service not found.
```

Причина — имя systemd unit отличается от имени каталога проекта.

Правильный service:

```text
news-bot.service
```

Правильно:

```bash
systemctl restart news-bot.service
```

---

# 42. Ошибка №2 — остановка процесса занимала слишком долго

При restart systemd был зафиксирован:

```text
State 'stop-sigterm' timed out.
Killing.
```

После этого старый процесс был завершён через SIGKILL.

Сервис затем успешно стартовал.

Вывод:

перезапуск работает, но shutdown приложения стоит дополнительно проверить и сделать корректным.

---

# 43. Ошибка №3 — schema code не совпала с legacy runtime DB

Одна из самых важных ошибок.

В исходном коде ожидалась более новая структура articles.

Код предполагал возможность:

```text
UNIQUE(event_id, kind, message_id)
```

но существующая production database содержала старую схему:

```text
event_id INTEGER UNIQUE
```

Это привело к:

```text
sqlite3.IntegrityError:
UNIQUE constraint failed: articles.event_id
```

Причина:

старый runtime database не был автоматически приведён к новой schema.

---

# 44. Ошибка №4 — FOREIGN KEY при искусственном UPDATE

После изменения теста возникла:

```text
sqlite3.IntegrityError:
FOREIGN KEY constraint failed
```

Причина была не в LLM, а в том, что тестовый UPDATE использовал искусственный:

```text
message_id=999999
```

которого не существовало в таблице messages.

А articles.message_id имеет foreign key.

То есть:

```text
articles.message_id
        ↓
messages.id
```

должен ссылаться на реально существующее сообщение.

Для тестов pipeline необходимо либо:

- использовать существующий message_id;
- либо создавать тестовое message в DB;
- либо тестировать LLM отдельно без записи в DB.

---

# 45. Ошибка №5 — тест UPDATE случайно пытался изменить DB

Первоначальный тест был направлен на проверку:

```text
classify_update
compose_event_update
```

но затем был вызван:

```text
pipeline.existing_event(...)
```

Этот метод уже выполняет реальные DB operations и может вызвать publisher.

Даже при Fake MAX:

```text
DB изменяется
```

если не остановить выполнение до стадии persistence.

Поэтому безопасный LLM тест должен быть:

```text
classify_update
      ↓
compose_event_update
      ↓
print result
```

без existing_event(), если задача — только проверить модель.

---

# 46. Ошибка №6 — Fake MAX защищает MAX, но не DB

Для тестирования был создан Fake MAX.

Это правильно защищает реальный MAX.

Но Fake MAX:

```text
не защищает SQLite
```

Pipeline всё равно может выполнить операции persistence.

Поэтому Fake publisher не является полноценной transaction sandbox.

---

# 47. Ошибка №7 — UPDATE classifier был слишком либеральным

Первоначальный prompt позволял модели считать существенными:

- новые цифры;
- дополнительные детали;
- подробности уже известного события.

Из-за этого повторное сообщение могло восприниматься как UPDATE.

Был добавлен более строгий prompt:

```text
Если сомневаешься между UPDATE и повтором — выбирай НЕ UPDATE.
```

Также были явно описаны признаки повторной информации.

---

# 48. Ошибка №8 — даже строгий LLM остаётся вероятностным

После ужесточения prompt тесты показали:

один запрос:

```text
important=true
score=75
```

другой запрос по очень похожей логике:

```text
important=false
score=45
```

Следствие:

нельзя использовать LLM score как единственный deterministic business rule.

Нужен дополнительный слой программных проверок.

---

# 49. Ошибка №9 — тест на абсурд

Был проведён специальный тест на намеренную галлюцинацию:

```text
После этого все участники мятежа всем скопом
полетели на Луну, чтобы убивать Гитлера.
```

Модель корректно классифицировала такой текст как:

```text
important=false
score=0
```

То есть явно абсурдные данные не должны превращаться в UPDATE.

Этот тест полезен как regression test для editorial layer.

---

# 50. Ошибка №10 — явные повторы между источниками

В production была обнаружена ситуация:

два сообщения фактически описывали одну и ту же новость.

При этом текст и формулировки отличались.

Это показало, что:

```text
exact hash
```

недостаточно.

Поэтому pipeline был расширен:

```text
exact hash
      ↓
lexical similarity
      ↓
semantic event resolver
```

Именно event-level deduplication является правильным уровнем для новостной системы.

---

# 51. Почему нельзя решать всё similarity

Простое сравнение текстов тоже недостаточно.

Например:

```text
Россия нанесла удар по объекту
```

и:

```text
Россия нанесла новый удар по тому же региону
```

могут иметь высокую текстовую похожесть, но описывать разные события.

Поэтому similarity должна быть только первым фильтром.

Финальное решение:

```text
semantic event resolution
```

---

# 52. Архитектурный принцип

Система должна использовать несколько уровней защиты:

```text
Level 1
Exact duplicate
        ↓
Level 2
Lexical similarity
        ↓
Level 3
Semantic event resolver
        ↓
Level 4
Editorial LLM
        ↓
Level 5
Deterministic publishing rules
        ↓
MAX
```

Ни один отдельный слой не должен считаться абсолютно надёжным.

---

# 53. Тестирование

В репозитории есть:

```text
tests/test_core.py
```

Также во время разработки использовались специальные production-safe smoke tests.

Важное правило:

тесты, которые проверяют LLM, не должны:

- публиковать в реальный MAX;
- менять production DB;
- создавать реальные публикации.

Для этого используются FakeMAX и/или тестовые сообщения.

---

# 54. Безопасный LLM test

Правильная форма:

```python
check = await llm.classify_update(
    previous_title,
    previous_text,
    fake_source,
)

if check["important"]:
    data = await llm.compose_event_update(
        previous_title,
        previous_text,
        fake_source,
    )

print(check)
print(data)
```

Без вызова:

```text
pipeline.existing_event(...)
```

если не требуется тестировать persistence.

---

# 55. Безопасный pipeline test

Если требуется тестировать весь pipeline:

```text
DB
 ↓
Pipeline
 ↓
Fake MAX
```

но тест должен использовать отдельные тестовые данные.

Нельзя подсовывать несуществующие foreign-key IDs.

---

# 56. Backup policy

Перед изменением важных production файлов создаётся backup.

Например:

```bash
cp newsbot/llm/adapter.py \
   newsbot/llm/adapter.py.bak-$(date +%Y%m%d-%H%M%S)
```

Для:

```text
db.py
adapter.py
pipeline.py
```

это полезно делать перед рискованными изменениями.

---

# 57. Deployment workflow

Рекомендуемый процесс:

```text
1. Изменить код
       ↓
2. Сделать backup
       ↓
3. py_compile
       ↓
4. локальный/safe test
       ↓
5. restart service
       ↓
6. проверить status
       ↓
7. проверить journalctl
       ↓
8. проверить DB
       ↓
9. git commit
       ↓
10. git push
```

---

# 58. Минимальная проверка после изменения

```bash
cd /opt/news-bot-v1

./.venv/bin/python -m py_compile \
  newsbot/db.py \
  newsbot/llm/adapter.py \
  newsbot/core/pipeline.py \
  newsbot/telegram/collector.py
```

Если compile проходит без ошибок:

```bash
systemctl restart news-bot.service
```

Проверить:

```bash
systemctl status news-bot.service --no-pager -l
```

И логи:

```bash
journalctl -u news-bot.service -n 100 --no-pager -o cat
```

---

# 59. Git workflow

На локальном ПК:

```bat
cd /d D:\Project\MAX\news-bot-git
```

Проверка:

```bat
git status
```

Commit:

```bat
git add .
git commit -m "Description of change"
```

Push:

```bat
git push
```

---

# 60. Production → Git snapshot

Если требуется сохранить текущее production состояние:

```text
server
  ↓
archive/snapshot
  ↓
local PC
  ↓
Git
  ↓
GitHub
```

При этом не переносить:

```text
.env
runtime/
.venv/
Telegram sessions
MAX sessions
```

---

# 61. Что Git должен содержать

Да:

```text
Python source
README
tests
requirements
sources.json
.env.example
systemd unit
deployment scripts
```

Нет:

```text
.env
runtime/news.db
runtime/max/
runtime/telegram/
.venv/
__pycache__/
*.pyc
Telegram sessions
MAX session database
```

---

# 62. Что является production state

Production state:

```text
runtime/
```

В частности:

```text
runtime/news.db
runtime/max/
runtime/telegram/
```

Это состояние конкретного сервера.

Оно не должно быть частью исходного репозитория.

---

# 63. Текущий baseline

Production snapshot от 29.08.2026 следует рассматривать как:

```text
GOLDEN BASELINE
```

Это означает:

- система уже работает;
- основные компоненты связаны;
- production pipeline проверен;
- GitHub содержит актуальный snapshot;
- дальнейшие изменения следует делать небольшими итерациями.

Не следует без необходимости переписывать всю систему целиком.

---

# 64. Что уже доказано production-тестами

В ходе разработки были подтверждены:

### Telegram

```text
Telegram connected
poll cycle started
poll source=...
```

### LLM

```text
LLM request provider=groq
HTTP 200
LLM provider=groq
```

### Dedup

В production появлялись события вида:

```text
DUPLICATE semantic event=...
```

что подтверждает работу event-level duplicate detection.

### MAX

Были подтверждены реальные публикации:

```text
MAX connected
message sent
MAX publish id=...
pipeline: NEW event=... max=...
```

### UPDATE

В тестовой среде была подтверждена цепочка:

```text
article kind=UPGRADE
publication kind=UPGRADE
parent_publication_id=<previous publication>
reply_to=<previous MAX message>
```

---

# 65. Что ещё требует развития

Несмотря на рабочий baseline, система ещё требует дальнейшего улучшения.

## 1. Deterministic UPDATE detection

Не полагаться исключительно на LLM.

Нужно дополнительно программно выделять:

```text
числа
статусы
погибшие
пострадавшие
задержанные
новые действия
новые последствия
```

---

## 2. DB migrations

Схема БД должна иметь полноценный migration mechanism.

Нельзя предполагать:

```text
код = schema
```

если на production уже существует legacy database.

Нужно:

```text
schema version
    ↓
migration
    ↓
current schema
```

---

## 3. Test isolation

Нужны отдельные:

```text
test DB
test MAX
test Telegram
```

чтобы тест pipeline никогда не изменял production state.

---

## 4. Graceful shutdown

Нужно устранить случаи:

```text
stop-sigterm timed out
```

и добиться нормального завершения collector/admin workers.

---

## 5. Observability

Стоит добавить структурированные события:

```text
EVENT_CREATED
EVENT_DUPLICATE
EVENT_UPDATE
UPDATE_REJECTED
ARTICLE_CREATED
PUBLICATION_CREATED
LLM_FAILED
TELEGRAM_FLOODWAIT
```

Это упростит диагностику.

---

# 66. Рекомендуемая будущая архитектура

Целевой вариант:

```text
                    ┌──────────────────┐
                    │ Telegram         │
                    │ Sources          │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Ingestion        │
                    │ Collector        │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Message Store    │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Dedup Engine     │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Event Resolver   │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Editorial Engine │
                    │                  │
                    │ LLM              │
                    │ + rules          │
                    └────────┬─────────┘
                             │
                     ┌───────┴────────┐
                     │                │
                   reject           publish
                                      │
                                      ▼
                              ┌──────────────┐
                              │ MAX Publisher│
                              └──────────────┘
```

Отдельно:

```text
                 Control Plane
                      │
                      ▼
               managed_sources
                      │
                      ▼
                  Collector
```

---

# 67. Главный архитектурный принцип проекта

News Bot не должен быть:

```text
Telegram → LLM → MAX
```

Правильная модель:

```text
Telegram
   ↓
Ingestion
   ↓
Storage
   ↓
Deduplication
   ↓
Event Resolution
   ↓
Editorial Decision
   ↓
Deterministic Validation
   ↓
Publication
   ↓
MAX
```

LLM — важная часть системы, но не единственный источник истины.

---

# 68. Итог

Проект уже прошёл путь от простого Telegram-to-MAX скрипта до многоуровневой новостной платформы.

Сейчас в системе есть:

- ingestion;
- persistent storage;
- duplicate detection;
- semantic event clustering;
- editorial LLM;
- provider fallback;
- confidence;
- priority;
- categories;
- NEW publications;
- UPDATE/UPGRADE publications;
- MAX integration;
- production systemd;
- logging;
- runtime source management;
- тестовая инфраструктура.

Главные проблемы, обнаруженные во время разработки:

- несоответствие legacy DB и новой schema;
- отсутствие изначально полноценной DB migration strategy;
- foreign-key ошибки в искусственных тестах;
- тесты, способные затронуть production DB;
- неправильное имя systemd service;
- долгий shutdown;
- слишком либеральная классификация UPDATE;
- вероятностное поведение LLM;
- недостаточная защита от повторов между разными источниками.

Главный вывод:

> LLM должен выполнять роль редактора и аналитика, но критические решения публикации должны дополнительно контролироваться deterministic-кодом.

Production snapshot от 29.08.2026 используется как baseline для дальнейшего развития проекта.
