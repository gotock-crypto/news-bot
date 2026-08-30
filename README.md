NEWS BOT v1 — ПОЛНАЯ ТЕХНИЧЕСКАЯ ДОКУМЕНТАЦИЯ
Дата: 30.08.2026

1. НАЗНАЧЕНИЕ
================

News Bot — автоматический новостной pipeline:

Telegram -> Collector -> SQLite -> Deduplication -> Event Resolver ->
LLM Editorial -> NEW / DUPLICATE / UPDATE -> MAX

Главный принцип:

    Новое сообщение Telegram != новая новость.

Несколько источников могут описывать одно реальное событие. Такие
материалы объединяются в один event. Существенное развитие события
публикуется как UPDATE.

2. АРХИТЕКТУРА
================

Telegram
  |
  v
Telegram Collector
  |
  v
SQLite / messages
  |
  v
Deduplication
  |
  v
Event Resolver
  |
  v
LLM Editorial
  |
  +-- NEW -------> MAX publication
  |
  +-- DUPLICATE -> ничего не публикуем
  |
  +-- UPDATE ----> MAX update / reply

Основные подсистемы:

- Telegram ingestion
- realtime events
- polling
- per-source watermark
- SQLite storage
- exact duplicate detection
- lexical similarity
- semantic event resolver
- LLM editorial processing
- Groq
- fallback provider
- category / priority / confidence
- publish decision
- NEW / UPDATE publication
- MAX publisher
- systemd
- journald
- tests

3. СТРУКТУРА ПРОЕКТА
=====================

news-bot-v1/
+-- .env.example
+-- .gitignore
+-- README.md
+-- deploy.sh
+-- main.py
+-- news-bot.service
+-- requirements.txt
+-- requirements-dev.txt
+-- sources.json
+-- newsbot/
|   +-- admin_bot.py
|   +-- app.py
|   +-- config.py
|   +-- db.py
|   +-- logging_setup.py
|   +-- core/
|   |   +-- dedup.py
|   |   +-- event_resolver.py
|   |   +-- pipeline.py
|   +-- llm/
|   |   +-- adapter.py
|   +-- max/
|   |   +-- publisher.py
|   +-- telegram/
|       +-- collector.py
+-- runtime/
+-- tests/
    +-- test_core.py
    +-- test_dedup_upgrade.py

4. КОМПОНЕНТЫ
==============

main.py
Главная точка запуска:

    python main.py

newsbot/app.py
Связывает основные компоненты приложения.

newsbot/config.py
Загрузка конфигурации из environment. Реальные секреты должны
находиться в .env. В Git хранится .env.example.

newsbot/db.py
SQLite persistence layer. Production DB:

    runtime/news.db

newsbot/core/dedup.py
Первичная дедупликация: нормализация, hash, lexical similarity.

newsbot/core/event_resolver.py
Определяет, описывают ли разные тексты одно и то же реальное событие.
Результат: new / duplicate / update.

newsbot/core/pipeline.py
Главная бизнес-логика: NEW / DUPLICATE / UPDATE / REJECT.

newsbot/llm/adapter.py
Единый интерфейс LLM.

newsbot/telegram/collector.py
Получение сообщений Telegram через MTProto / Telethon.
Используются realtime events, polling, watermark, FloodWait handling,
timeout, cooldown и source isolation.

newsbot/max/publisher.py
Публикация материалов в MAX.

5. TELEGRAM COLLECTOR
======================

Используются realtime и polling.

Realtime:

    Telegram update -> collector -> pipeline

Polling является safety-net: если realtime update был потерян,
сообщение может быть найдено при следующей проверке.

6. WATERMARK
=============

Для каждого источника хранится последний обработанный Telegram
message ID.

Примеры из production:

    rian_ru   -> 345119
    mash      -> 77353
    shot_shot -> 99477

Следующая проверка запрашивает сообщения после watermark.

7. ИЗОЛЯЦИЯ ИСТОЧНИКОВ
=======================

В production наблюдались timeout:

    poll source=bazabazon timeout=45s
    poll source=mod_russia timeout=45s
    poll source=rt_russian timeout=45s
    poll source=tass_agency timeout=45s

Правильное поведение:

    проблемный source -> cooldown / isolated
    остальные sources -> продолжают работу

Ошибка одного Telegram-источника не должна останавливать весь bot.

8. FLOODWAIT И RATE LIMITS TELEGRAM
====================================

Используются:

- watermark
- запрос только новых сообщений
- FloodWait handling
- cooldown
- source isolation

Не следует агрессивно запрашивать всю историю каналов на каждом цикле.

9. ИСТОЧНИКИ
============

Конфигурация:

    sources.json

Параметры источника:

    username
    enabled
    priority
    category
    reliability

Priority и reliability — разные параметры.

Priority = редакционная важность источника.
Reliability = ожидаемая надёжность информации.

10. DEDUPLICATION
=================

Pipeline:

    message
      |
      v
    exact duplicate
      |
      v
    lexical similarity
      |
      v
    event candidates
      |
      v
    semantic resolver

Цель — не превращать несколько сообщений об одном событии
в несколько независимых публикаций.

11. EVENT RESOLVER
===================

Resolver работает на уровне реального события.

Например:

A: «Самолёт Cessna угнан в Венгрии.»

B: «24-летний мужчина угнал Cessna, направлявшийся к АЭС "Пакш".»

Формулировки разные, но событие одно.

12. RESOLVER SCORE
==================

Поле top — кандидатная метрика, а не финальное решение.

Например:

    resolver candidates=20
    top=0.539

После этого LLM может определить:

    relation=duplicate

Используется комбинация:

- exact match
- lexical similarity
- candidate ranking
- semantic LLM decision

13. NEW
=======

Если материал описывает новое событие:

    relation=new

создаётся новый event:

    NEW event=485

После публикации событие получает MAX publication ID.

14. DUPLICATE
=============

Если материал относится к известному событию и новых существенных
фактов нет:

    relation=duplicate

Новый MAX-пост не создаётся.

Пример:

    relation=duplicate
    event=480
    confidence=0.98
    new_facts=False

15. UPDATE
==========

UPDATE нужен при существенном развитии уже известного события.

Например:

NEW:
    произошла атака

UPDATE:
    появились новые подтверждённые сведения о жертвах,
    разрушениях или существенном изменении масштаба.

Старый пост не редактируется — создаётся отдельная публикация.

16. ЦЕПОЧКА UPDATE
==================

    NEW
      |
      v
    UPDATE
      |
      v
    UPDATE

В БД используется:

    parent_publication_id

Это сохраняет историю развития события.

17. ПРОТИВОРЕЧИВЫЕ ЦИФРЫ
=========================

В production был случай:

    ранее: 172 спасённых
    новое сообщение: 237 спасённых

Resolver:

    relation=update
    confidence=0.95
    new_facts=True

Первый UPDATE classifier:

    important=False
    score=15

Он посчитал противоречивую цифру недостаточно надёжной
для отдельной публикации.

Позже другой материал дал:

    important=True
    score=75

После этого UPDATE был принят.

Вывод:

    изменение цифры само по себе не должно автоматически
    приводить к публикации UPDATE.

18. ПОЧЕМУ SCORE=15 НЕ ОБЯЗАТЕЛЬНО BUG
=======================================

UPDATE score — редакционная значимость, а не confidence.

Условно:

    score=10 -> полный повтор
    score=15 -> небольшое или сомнительное уточнение
    высокий score -> существенное изменение

Существенными могут быть:

- новые жертвы;
- существенное изменение масштаба;
- подтверждение ранее неподтверждённого факта;
- изменение статуса;
- новый важный этап;
- официальное подтверждение.

19. НЕ ОБХОДИТЬ ЗАЩИТУ ОТ ПРОТИВОРЕЧИВЫХ ЦИФР
================================================

Если было:

    5 пострадавших

а новый источник сообщает:

    3 пострадавших

это не означает автоматически UPDATE.

Нужно проверить:

1. одна ли это группа людей;
2. является ли это уточнением;
3. не относятся ли цифры к разным категориям;
4. какой источник сообщил каждую цифру;
5. появилась ли подтверждённая новая информация;
6. меняет ли это понимание события.

Консервативная политика здесь полезна.

20. LLM EDITORIAL
=================

LLM определяет:

- category
- priority
- confidence
- publish
- title
- text

Пример:

    category=world
    priority=8
    confidence=0.85
    publish=True

Другой пример:

    category=other
    priority=0
    confidence=0.90
    publish=False

21. GROQ
========

Production:

    provider = Groq
    model = qwen/qwen3.8-27b

22. FALLBACK И 429
==================

В production наблюдалось:

    HTTP/1.1 429 Too Many Requests

SDK выполнял retry.

Resolver также мог получить:

    resolver provider failed
    provider=groq
    error=429

Правильная стратегия:

    LLM unavailable
      |
      v
    retry / fallback
      |
      v
    pipeline continues

Причины 429:

- burst traffic;
- параллельные запросы;
- ограничения аккаунта;
- ограничения модели;
- несколько LLM-участков;
- retry самого SDK.

23. LLM МОЖЕТ ЗАМЕДЛИТЬ POLLING
================================

Типичная цепочка:

    poll
      |
      v
    LLM request
      |
      v
    429
      |
      v
    sleep
      |
      v
    retry
      |
      v
    длинный poll cycle

Поэтому LLM latency — один из основных факторов задержки.

24. SQLITE
==========

Production DB:

    runtime/news.db

Таблицы:

    admin_actions
    admin_stats
    articles
    event_messages
    event_updates
    event_window
    events
    managed_sources
    messages
    publications
    sources
    system_state

25. СХЕМА messages
===================

    id
    source
    source_message_id
    created_at
    text
    url
    media_path
    raw_json
    norm_hash
    source_priority
    source_reliability
    source_category
    priority
    reliability
    category

26. СХЕМА events
=================

    id
    created_at
    updated_at
    title
    category
    priority
    confidence
    status
    published_at
    max_message_id
    last_max_message_id

27. event_messages
===================

Связь:

    event_id
    message_id

Пример:

    message 100 ----+
    message 101 ----+---- event 480
    message 102 ----+

28. articles
============

Редакционные материалы:

    event_id
    message_id
    kind
    title
    text
    confidence
    reason
    created_at

kind позволяет различать NEW и UPDATE.

29. event_updates
==================

История UPDATE:

    event_id
    source_message_id
    created_at
    title
    text
    max_message_id

30. publications
=================

Факт публикации:

    event_id
    article_id
    kind
    max_message_id
    parent_publication_id
    created_at

31. EVENT WINDOW
================

Resolver работает с ограниченным временным окном:

    новое сообщение
      |
      v
    поиск похожих событий
      |
      v
    недавние events

Это ускоряет resolver и уменьшает количество ложных совпадений.

32. SQLITE DICT FIX
====================

Python dict/list сериализуются:

    dict/list -> JSON -> SQLite TEXT

При чтении выполняется обратное преобразование.

33. RETENTION
=============

Production DB не предназначена для бесконечного накопления истории.

В production snapshot использовалось:

    RETENTION_DAYS=4

При изменении retention нужно учитывать:

- event resolver;
- event window;
- UPDATE history;
- watermark;
- debugging requirements.

34. КОНФИГУРАЦИЯ
===============

Основные параметры могут включать:

    DB_FILE=runtime/news.db
    SOURCE_CONFIG=sources.json
    AUTO_PUBLISH=1
    MIN_PRIORITY_PUBLISH=3
    MIN_CONFIDENCE_PUBLISH=0.50
    BACKFILL_LIMIT=5
    POLL_INTERVAL=300
    DEDUP_HOURS=36
    SIMILARITY_THRESHOLD=0.86
    MIN_SOURCES_FOR_HIGH_CONFIDENCE=2
    RETENTION_DAYS=4
    LANGUAGE=ru
    MAX_POST_LENGTH=500

Фактические значения production определяются текущим .env.

35. БЕЗОПАСНЫЙ РЕЖИМ
====================

Для тестирования:

    AUTO_PUBLISH=0

Порядок:

    AUTO_PUBLISH=0
      -> проверка логов
      -> проверка SQLite
      -> проверка dedup
      -> проверка resolver
      -> проверка LLM
      -> AUTO_PUBLISH=1

36. ЛОКАЛЬНЫЙ ЗАПУСК WINDOWS
=============================

    py -3 -m venv .venv
    .venv\Scripts\activate
    python -m pip install -U pip
    pip install -r requirements.txt

Создать .env:

    copy .env.example .env

Запуск:

    python main.py

37. TELEGRAM AUTHORIZATION
===========================

Используется пользовательская MTProto session, а не обычный
Telegram Bot API.

При первом запуске может потребоваться:

- номер телефона;
- код Telegram;
- пароль 2FA.

Session:

    runtime/telegram/

Session files нельзя коммитить в Git.

38. MAX AUTHORIZATION
=====================

MAX publisher использует runtime/session состояние:

    runtime/max/

Эти файлы нельзя коммитить в Git.

39. PRODUCTION
==============

Production directory:

    /opt/news-bot-v1

Systemd:

    news-bot.service

Статус:

    systemctl status news-bot.service

Рестарт:

    systemctl restart news-bot.service

Логи:

    journalctl -u news-bot.service -f

40. JOURNALD ВМЕСТО app.log
============================

Production logging идёт через systemd/journald.

Поэтому команда:

    awk ... app.log

может вернуть:

    cannot open "app.log"

Правильный источник:

    journalctl -u news-bot.service

41. ЛОГИ ЗА ПЕРИОД
==================

    journalctl -u news-bot.service       --since "2026-08-30 12:39:00"       --until "2026-08-30 13:00:00"       -o cat

42. ОНЛАЙН ЛОГИ
==============

Последние 5 минут + продолжение в realtime:

    journalctl -u news-bot.service --since "5 minutes ago" -f -o cat

43. ТОЛЬКО ПУБЛИКАЦИИ
=====================

    journalctl -u news-bot.service -o cat | grep -E 'pipeline: (NEW|UPGRADE)'

За период:

    journalctl -u news-bot.service       --since "2026-08-30 12:39:00"       -o cat       | grep -E 'pipeline: (NEW|UPGRADE)'

44. RESOLVER LOGS
=================

    journalctl -u news-bot.service -o cat | grep 'event_resolver'

Ключевые строки:

    resolver candidates=
    resolver result relation=
    resolver provider failed

45. LLM LOGS
============

    journalctl -u news-bot.service -o cat | grep 'llm:'

Интересуют:

    provider
    model
    category
    priority
    confidence
    publish

46. UPDATE LOGS
===============

    journalctl -u news-bot.service -o cat       | grep -E 'UPDATE check|UPDATE decision|UPDATE rejected|UPGRADE'

47. 429 LOGS
============

    journalctl -u news-bot.service -o cat | grep '429'

48. TIMEOUT LOGS
================

    journalctl -u news-bot.service -o cat       | grep -E 'timeout|cooldown|isolated'

49. КОНКРЕТНЫЙ EVENT
====================

Например event 472:

    journalctl -u news-bot.service -o cat | grep 'event=472'

50. ПОЛУЧЕНИЕ ТЕКСТА ПУБЛИКАЦИЙ ИЗ БД
======================================

sqlite3 CLI на минимальном Ubuntu может отсутствовать.
Используйте Python из .venv:

    cd /opt/news-bot-v1

    ./.venv/bin/python -c "import sqlite3; con=sqlite3.connect('runtime/news.db'); rows=con.execute('SELECT e.id,e.published_at,a.kind,a.title,a.text FROM articles a JOIN events e ON e.id=a.event_id WHERE e.published_at IS NOT NULL ORDER BY e.published_at'); [print('='*80, f'\nEVENT: {r[0]}\nTIME: {r[1]}\nKIND: {r[2]}\nTITLE: {r[3]}\n\n{r[4]}') for r in rows]; con.close()"

51. SQLITE БЕЗ sqlite3 CLI
===========================

Если:

    sqlite3: command not found

используйте:

    cd /opt/news-bot-v1
    ./.venv/bin/python -c "import sqlite3; print(sqlite3.connect('runtime/news.db').execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())"

52. ДИАГНОСТИКА DUPLICATE
==========================

Если новость не опубликовалась:

    journalctl -u news-bot.service -o cat       | grep -E 'resolver result|UPDATE check|DUPLICATE|UPDATE rejected'

Смотреть:

- relation;
- event;
- confidence;
- new_facts;
- score;
- reason.

53. ДИАГНОСТИКА НЕПРАВИЛЬНОГО NEW
===================================

Если два материала должны быть одним событием, но создан новый event:

проверить:

- resolver candidates;
- top score;
- candidate event;
- LLM resolver result.

Особенно важны:

    candidates=0

или:

    top=0.xxx

Это может означать, что существующий event не попал в candidate window.

54. ДИАГНОСТИКА НЕПРАВИЛЬНОГО DUPLICATE
========================================

Проверить:

- resolver confidence;
- new_facts;
- UPDATE score;
- reason.

Важно отличать:

    одно событие

от:

    одинакового текста.

55. UPDATE POLICY
==================

Основной принцип:

    UPDATE публикуется только при наличии существенного нового факта.

Resolver отвечает:

    Это развитие того же события?

UPDATE classifier отвечает:

    Достаточно ли это важно для отдельной публикации?

Даже:

    relation=update
    confidence=0.95
    new_facts=True

не гарантирует публикацию.

56. НЕ ДЕЛАТЬ ТАК
=================

Нельзя безусловно делать:

    if resolver_relation == "update":
        publish()

Иначе мелкие уточнения превратятся в поток UPDATE.

Нужна комбинация:

    высокий resolver confidence
    +
    new_facts=True
    +
    достаточный UPDATE score
    +
    реальная редакционная значимость

57. OBSERVABILITY
=================

Главные показатели:

- poll cycle duration;
- новые сообщения по источникам;
- LLM latency;
- LLM 429;
- resolver candidates;
- resolver confidence;
- NEW;
- DUPLICATE;
- UPDATE;
- REJECT;
- публикации.

Для каждого решения желательно иметь:

    reason
    confidence
    score
    resolver relation
    resolver confidence
    new_facts

Цель:

    По логам должно быть возможно понять,
    почему конкретная новость была или не была опубликована.

58. ТЕСТИРОВАНИЕ
================

Запуск:

    pytest

Основные тесты:

    tests/test_core.py
    tests/test_dedup_upgrade.py

Перед изменениями dedup/resolver/pipeline/adapter/collector
желательно запускать тесты.

59. ПОРЯДОК ИЗМЕНЕНИЯ PRODUCTION
=================================

Рекомендуемый процесс:

1. backup
2. изменение
3. syntax check
4. tests
5. restart service
6. journalctl
7. smoke test
8. наблюдение

Не менять production вслепую.

60. BACKUP
==========

Критические компоненты:

    event_resolver.py
    pipeline.py
    collector.py
    adapter.py

Backup production и Git history — разные механизмы.

Файлы:

    *.bak
    *.bak-*

не должны попадать в Git.

61. PRODUCTION ARCHIVE
======================

Snapshot может называться:

    news-bot-v1-full-YYYYMMDD-HHMMSS.tar.gz

Он может содержать:

- source code;
- runtime;
- database;
- media;
- sessions;
- configuration.

Production archive нельзя автоматически загружать в GitHub.

62. GIT И PRODUCTION
====================

Git должен хранить исходный код и документацию.

В Git:

- Python source;
- requirements;
- README;
- tests;
- deploy files;
- .env.example;
- configuration templates.

Не в Git:

- .env;
- production DB;
- SQLite WAL/SHM;
- Telegram sessions;
- MAX sessions;
- media;
- backup files;
- credentials.

63. БЕЗОПАСНОСТЬ
================

Реальные:

- API keys;
- Telegram credentials;
- MAX credentials;
- admin tokens;
- session files

никогда не должны попадать в:

- README;
- Git;
- GitHub issues;
- публичные архивы;
- логи;
- сообщения в чатах.

Если credential был случайно опубликован, его следует считать
потенциально скомпрометированным и при необходимости ротировать.

64. GIT NON-FAST-FORWARD
========================

Если git push возвращает:

    non-fast-forward

локальная и удалённая ветки имеют разные вершины истории.

Сначала:

    git fetch origin

Проверка:

    git log --oneline --left-right --graph main...origin/main

Если истории независимы:

    git merge origin/main --allow-unrelated-histories

После конфликтов:

    git add .
    git commit
    git push

65. PRODUCTION SNAPSHOT + GITHUB HISTORY
=========================================

При синхронизации production snapshot с существующей GitHub history
локальная и удалённая истории были независимыми.

Был выполнен merge с:

    --allow-unrelated-histories

Возникли add/add conflicts в:

    .gitignore
    README.md
    newsbot/config.py
    newsbot/core/event_resolver.py
    newsbot/core/pipeline.py
    newsbot/llm/adapter.py
    newsbot/telegram/collector.py

В качестве разрешения использовалась production-версия локальной ветки
через:

    git checkout --ours -- <files>

После:

    git add ...
    git commit -m "Merge GitHub history into production snapshot"

Итоговая вершина:

    eec63f0 Merge GitHub history into production snapshot

66. PRODUCTION КАК SOURCE OF TRUTH
===================================

Если GitHub содержит старую версию, а production — актуальную рабочую
версию, сначала определить источник истины.

Для production snapshot:

    production code -> source of truth

Историю GitHub при этом не обязательно удалять.

67. WINDOWS: WILDCARD В TAR
============================

В Windows CMD команда:

    tar -xzf news-bot-v1-full-*.tar.gz -C news-bot-v1-archive

может не сработать.

Если:

    Failed to open 'news-bot-v1-full-*.tar.gz'

использовать точное имя:

    tar -xzf news-bot-v1-full-20260830-172214.tar.gz -C news-bot-v1-archive

68. WINDOWS CMD И СПЕЦСИМВОЛЫ
==============================

Символы &, |, >, < имеют специальное значение в CMD/bash.

Не вставлять случайные символы в командную строку.

69. ИЗВЕСТНЫЕ PRODUCTION ПРОБЛЕМЫ
==================================

A. Groq 429
-----------
Симптом:

    HTTP/1.1 429 Too Many Requests

Проверить retry, fallback и продолжение pipeline.

B. Telegram timeout
-------------------
Симптом:

    poll source=... timeout=45s

Правильное поведение:

    source isolated

Проверить, что остальные sources продолжают работу.

C. Длинный poll cycle
---------------------
Например:

    poll cycle=26 finished duration=45.08s

Проверять:

- LLM 429;
- retry sleep;
- Telegram timeout;
- конкретный source.

D. DUPLICATE score=15
---------------------
Это не обязательно bug. Classifier может считать небольшое
или противоречивое уточнение недостаточно надёжным.

E. resolver update, но UPDATE rejected
---------------------------------------
Это возможно и нормально:

Resolver отвечает за отношение к событию.
UPDATE classifier — за редакционную значимость.

F. app.log отсутствует
----------------------
Production использует journald:

    journalctl -u news-bot.service

G. sqlite3 command not found
----------------------------
Использовать стандартный Python module sqlite3 через .venv.

70. RECOMMENDED DEBUG FLOW
===========================

Если:

    «Почему новость не опубликовалась?»

Порядок:

1. Найти входящее сообщение.
2. Найти LLM classification.
3. Найти resolver result.
4. Определить event.
5. Если duplicate/update — найти UPDATE check.
6. Посмотреть score.
7. Посмотреть reason.
8. Проверить, был ли уже опубликован event.
9. Проверить 429/retry.
10. Проверить SQLite.

Если:

    «Почему появилась новая публикация, хотя это тот же сюжет?»

Порядок:

1. Найти NEW event.
2. Найти source message.
3. Посмотреть resolver candidates.
4. Посмотреть top.
5. Посмотреть relation.
6. Проверить, существовал ли старый event в candidate window.
7. Если candidates=0 — проверить event window/retention.
8. Если resolver ошибся — анализировать semantic decision.

71. GIT CHECKLIST
=================

Перед push:

    git status

Проверить отсутствие:

    .env
    *.db
    *.sqlite
    *.sqlite3
    *.session
    runtime/
    *.bak

Затем:

    git add .
    git commit -m "..."
    git push

После:

    git status

Ожидаемо:

    nothing to commit, working tree clean

72. PRODUCTION CHECKLIST
========================

После деплоя:

    systemctl status news-bot.service

    journalctl -u news-bot.service -n 100 -o cat

Polling:

    journalctl -u news-bot.service -o cat | grep 'poll source='

LLM:

    journalctl -u news-bot.service -o cat | grep 'LLM provider='

Resolver:

    journalctl -u news-bot.service -o cat | grep 'resolver result'

Публикации:

    journalctl -u news-bot.service -o cat | grep -E 'pipeline: (NEW|UPGRADE)'

73. ГЛАВНЫЙ ПРИНЦИП РЕДАКЦИОННОГО PIPELINE
============================================

Система не должна считать:

    новый текст = новая новость

Правильная логика:

    новый текст
      |
      v
    что произошло?
      |
      v
    какое событие?
      |
      v
    было ли оно уже?
      |
      v
    есть ли новые существенные факты?
      |
      v
    достаточно ли доверия?
      |
      v
    имеет ли материал редакционную ценность?
      |
      v
    публиковать?

74. ХОРОШЕЕ И ПЛОХОЕ ПОВЕДЕНИЕ
================================

Хороший результат:

    10 сообщений -> 3 события -> 3 NEW -> 1 UPDATE

Плохой результат:

    10 сообщений -> 10 публикаций

Также плохой результат:

    одно событие -> 5 противоречивых UPDATE

Цель — минимизировать оба типа ошибок.

75. ГЛАВНЫЕ АРХИТЕКТУРНЫЕ ПРИНЦИПЫ
===================================

Fail-safe:
ошибка одного компонента не должна уничтожать весь pipeline.

Conservative publishing:
сомнительное уточнение лучше не публиковать, чем выдать
неподтверждённую информацию как факт.

Event-based deduplication:
дедупликация должна происходить на уровне событий, а не только строк.

Source attribution:
заявление источника должно оставаться заявлением источника.

Auditability:
по логам должно быть понятно, почему публикация состоялась
или была отклонена.

Runtime separation:
код и runtime разделены.

Secrets separation:
секреты никогда не должны находиться в Git.

76. РЕКОМЕНДАЦИИ ПО РАЗВИТИЮ
=============================

1. LLM reliability:
   - rate limiting
   - provider fallback
   - retry policy
   - timeout
   - observability

2. Event Resolver:
   - candidate ranking
   - event window
   - confidence
   - ambiguous cases

3. UPDATE policy:
   не пропускать важные обновления, но не превращать канал
   в поток дублей.

4. Observability:
   - NEW / DUPLICATE / UPDATE / REJECT
   - 429 rate
   - resolver confidence
   - average LLM latency
   - source timeout rate

77. ТЕКУЩАЯ GIT HISTORY
=======================

На момент подготовки документации production snapshot был объединён
с существующей GitHub history.

Текущая вершина:

    eec63f0 Merge GitHub history into production snapshot

История:

    eec63f0 Merge GitHub history into production snapshot
    |
    +-- 2f1904a Update README.md
    +-- 68fdac6 Update README.md
    +-- 82c5eea Clean production snapshot
    +-- d91255b Sync production v2 snapshot
    |
    +-- b8b0e13 Remove obsolete backup file
    +-- 49e4986 Archive current production project

78. ИТОГ
========

News Bot — не простой Telegram -> MAX forwarder.

Это редакционный pipeline:

Telegram
  -> Ingestion
  -> Storage
  -> Deduplication
  -> Event Resolution
  -> Editorial LLM
  -> NEW / DUPLICATE / UPDATE
  -> Publication
  -> MAX

Главная ценность:

- не пропускать важное;
- не публиковать повторы;
- не превращать сомнительные уточнения в факты;
- сохранять развитие событий;
- переживать сбои внешних сервисов;
- сохранять объяснимость решений.

Главный критерий качества:

    Каждая опубликованная новость должна иметь понятную причину,
    почему она заслуживает отдельной публикации.

КОНЕЦ ДОКУМЕНТА
