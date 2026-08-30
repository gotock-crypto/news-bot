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
- автоматическое решение `publish / reject`;
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
