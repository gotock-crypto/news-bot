# News Bot

AI-powered news monitoring and cross-platform publishing pipeline.

Система автоматически получает новые публикации из Telegram-источников, анализирует их с помощью LLM, определяет категорию, приоритет и отношение к уже известным событиям, после чего публикует подходящие материалы в MAX.

Проект работает в production на Linux/VPS и рассчитан на непрерывную работу без ручного вмешательства.

---

## Что делает система

Основной pipeline:

```text
Telegram channels
       │
       ▼
Telegram Collector
       │
       ├── LIVE updates
       └── polling safety-net
       │
       ▼
SQLite
       │
       ▼
LLM analysis
       │
       ├── category
       ├── priority
       ├── confidence
       └── publish decision
       │
       ▼
Event Resolver
       │
       ├── NEW
       ├── DUPLICATE
       └── UPDATE
       │
       ▼
Editorial decision
       │
       ▼
MAX Publisher
       │
       └── MAX channel
