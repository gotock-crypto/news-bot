# News Bot v1

Полноценный MVP новостника для MAX: Telegram collector -> SQLite -> dedup/event clustering -> Groq/Mistral editor -> PyMax publisher.

Архитектура собрана с учётом практик из загруженных проектов:
- BeauQuot 3.1.6: конфигурация через `.env`, SQLite, fail-safe внешние AI-вызовы, подробное логирование.
- BeauHoroscope 4.0: единый LLM adapter, Groq primary + Mistral fallback, `LLM_LOCK`.
- Minerals 2.2.6: media-first публикация и разделение медиа/текста.

## 1. Установка Windows

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
```

## 2. Telegram API

Создайте API ID/API hash в официальном Telegram API portal. Это обычная пользовательская MTProto-сессия, не Bot API.

Заполните `.env` по `.env.example`.

Первый запуск `python main.py` попросит код Telegram и пароль 2FA, если он включён. Сессия сохранится в `runtime/telegram/`.

## 3. MAX

В `.env` укажите тот же `MAX_PHONE`, с которым была успешная авторизация PyMax PoC. Скопируйте созданную в PoC SQLite-сессию `max.db` в `runtime/max/` или укажите другой `MAX_WORK_DIR`.

Не кладите session DB в git.

## 4. Источники

В `sources.json` добавьте реальные публичные usernames каналов без `@` и поставьте `enabled: true`.

Пример:

```json
{"username":"some_channel","enabled":true,"priority":10,"category":"politics"}
```

Система при старте делает backfill последних `BACKFILL_LIMIT` сообщений каждого источника, затем продолжает слушать новые сообщения в реальном времени.

## 5. Режимы

`AUTO_PUBLISH=0` — безопасный режим: статьи генерируются и попадают в очередь, но в MAX не публикуются.

`AUTO_PUBLISH=1` — автоматическая публикация только после прохождения дедупликации и редакторской валидации.

Рекомендованный запуск:

1. 1–2 часа `AUTO_PUBLISH=0`.
2. Проверить `runtime/news.db` и логи.
3. Настроить источники и стиль.
4. Затем включить `AUTO_PUBLISH=1`.

## 6. Что делает дедупликация

1. exact hash нормализованного текста;
2. lexical similarity с недавно увиденными материалами;
3. event clustering: похожие сообщения объединяются в один `news_event`;
4. повторная публикация блокируется по `event_id`.

Перепечатки из разных каналов не считаются независимыми источниками только из-за разного текста.

## 7. Категории

Политика и СВО входят в основной приоритет. Остальные категории можно оставить включёнными для контекста; публикация определяется editorial prompt и приоритетом.

## 8. Запуск

```bat
copy .env.example .env
notepad .env
notepad sources.json
python main.py
```

В текущей версии MAX-публикатор отправляет текст и при наличии локального изображения — фото + текст. Для PyMax это делается через `Photo(path=...)`.
