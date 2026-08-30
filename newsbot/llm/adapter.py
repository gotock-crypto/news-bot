import asyncio
import json
import logging
import re
import time

import requests

try:
    from groq import Groq
except ImportError:
    Groq = None


log = logging.getLogger("llm")
LOCK = asyncio.Lock()
LLM_MIN_INTERVAL = 4.0
LAST_REQUEST_AT = 0.0


SYSTEM = """
Ты — главный редактор русского новостного канала.

Твоя задача:
1. Отделять проверяемые факты от оценок, слухов и эмоций.
2. Определять, является ли сообщение полноценной новостью.
3. Если несколько источников описывают одно событие — объединять их.
4. Не выдумывать факты, цифры, имена, даты, места или цитаты.
5. Не усиливать формулировки относительно исходных источников.
6. Не копировать исходный текст дословно.
7. Писать коротко, естественно и по-новостному.
7a. Для публикации: заголовок в одну строку; текст обычно 2 коротких абзаца, максимум 3. Цель — 300–500 знаков. Не стремись заполнить лимит: чем короче можно передать новость без потери смысла, тем лучше. Жёсткий максимум — 500 знаков.
7b. Не повторяй заголовок первым предложением. Убирай исторический фон, очевидные пояснения, второстепенные детали и длинные перечисления. Сохраняй только 2–4 наиболее важных факта. Не используй эмодзи, декоративные разделители и кликбейт.
7c. Можно выделять 1–2 ключевых числа или факта Telegram-разметкой **жирным**, но не перегружай форматирование.
8. Не использовать пропагандистские лозунги, эмоциональную истерику или оскорбления.
9. Если информация подтверждена только одним источником, учитывать это при confidence.
10. Официальное заявление государственного ведомства или должностного лица является самостоятельным источником информации: отсутствие второго независимого подтверждения само по себе НЕ является основанием для publish=false. В таком случае явно отражай атрибуцию в тексте (например: «Минобороны РФ заявило...»), не превращая заявление в независимо установленный факт.
11. Если источники противоречат друг другу — не придумывать, кто прав.
12. Не публиковать рекламу, поздравления, мемы, бытовые посты и очевидный флуд.
12. Основные категории:
   politics, svo, russia, world, economy, security

Очень важно:
- Не добавляй сведения, которых нет в предоставленных источниках.
- Не выдавай предположение за установленный факт.
- Не называй информацию подтвержденной, если подтверждения нет.
- Если событие действительно новостное, подготовь самостоятельный рерайт.
- Текст должен быть на русском языке.

Верни ТОЛЬКО JSON без markdown:

{
  "publish": true,
  "title": "...",
  "text": "...",
  "category": "politics|svo|russia|world|economy|security|other",
  "priority": 0,
  "confidence": 0.0,
  "reason": "..."
}

publish=false используй для:
- не новости;
- неподтвержденного слуха;
- дубликата;
- рекламы;
- мнения без новостного события;
- сообщения, не относящегося к основным направлениям;
- ситуации, когда фактов недостаточно для нормального новостного материала.
"""


class LLM:
    def __init__(self, s):
        self.s = s

    # ---------------------------------------------------------
    # Groq через официальный Python SDK
    # ---------------------------------------------------------

    def _call_groq(self, key, model, max_tokens, temperature, prompt):
        if Groq is None:
            raise RuntimeError(
                "Пакет groq не установлен. Выполни: pip install -U groq"
            )

        client = Groq(api_key=key)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        if not response.choices:
            raise RuntimeError("Groq returned no choices")

        content = response.choices[0].message.content

        if not content:
            raise RuntimeError("Groq returned empty content")

        return content

    # ---------------------------------------------------------
    # Mistral fallback
    # ---------------------------------------------------------

    def _call_mistral(
        self,
        key,
        model,
        max_tokens,
        temperature,
        prompt,
    ):
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": SYSTEM,
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=self.s.llm_timeout,
        )

        response.raise_for_status()

        data = response.json()

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Invalid Mistral response: {data}"
            ) from exc

    # ---------------------------------------------------------
    # JSON parser
    # ---------------------------------------------------------

    @staticmethod
    def _parse(raw):
        if not raw:
            raise ValueError("LLM returned empty response")

        raw = raw.strip()

        # Убираем markdown fences.
        raw = re.sub(
            r"^```(?:json)?\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )

        raw = re.sub(
            r"\s*```$",
            "",
            raw,
        )

        raw = raw.strip()

        # Сначала пробуем обычный JSON.
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Если модель добавила пояснение вокруг JSON,
        # достаем первый JSON-объект.
        start = raw.find("{")
        end = raw.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"LLM response does not contain JSON object: {raw[:500]}"
            )

        candidate = raw[start:end + 1]

        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON from LLM: {candidate[:1000]}"
            ) from exc

    # ---------------------------------------------------------
    # Normalization / validation
    # ---------------------------------------------------------

    def _normalize(self, data):
        if not isinstance(data, dict): raise ValueError("LLM JSON must be an object")
        publish=data.get("publish",False)
        if isinstance(publish,str): publish=publish.strip().lower() in {"true","1","yes","да"}
        data["publish"]=bool(publish); data["title"]=str(data.get("title","")).strip(); data["text"]=str(data.get("text","")).strip()
        data["category"]=str(data.get("category","other")).strip().lower()
        if data["category"] not in {"politics","svo","russia","world","economy","security","other"}: data["category"]="other"
        try: data["priority"]=max(0,min(100,int(data.get("priority",0))))
        except (TypeError,ValueError): data["priority"]=0
        try: data["confidence"]=max(0.0,min(1.0,float(data.get("confidence",0))))
        except (TypeError,ValueError): data["confidence"]=0.0
        data["reason"]=str(data.get("reason","")).strip()
        if not data["title"] or not data["text"]:
            data["publish"]=False; data["reason"]=data["reason"] or "LLM не сформировал полный материал"
        max_length=getattr(self.s,"max_post_length",500)
        if len(data["text"])>max_length: data["text"]=data["text"][:max_length-3].rstrip()+"..."
        return data

    # ---------------------------------------------------------
    # Формирование prompt
    # ---------------------------------------------------------

    @staticmethod
    def _build_prompt(sources):
        blocks = []

        for source in sources:
            source_name = source.get(
                "source",
                "unknown",
            )

            reliability = source.get(
                "reliability",
                0.7,
            )

            url = source.get(
                "url",
                "",
            )

            text = source.get(
                "text",
                "",
            )

            blocks.append(
                "\n".join(
                    [
                        f"ИСТОЧНИК: {source_name}",
                        f"НАДЕЖНОСТЬ: {float(reliability):.2f}",
                        f"URL: {url}",
                        "ТЕКСТ:",
                        text,
                    ]
                )
            )

        joined = "\n\n--- ИСТОЧНИК ---\n\n".join(
            blocks
        )

        return f"""
Ниже приведены сообщения из Telegram-источников.

Определи, описывают ли они одно реальное новостное событие.

Если да:
- выдели только подтверждаемые факты;
- подготовь самостоятельный русский новостной текст;
- не копируй исходные формулировки;
- укажи степень уверенности;
- выбери категорию;
- назначь приоритет.
- Если единственный источник — официальное заявление ведомства/должностного лица, публикация допустима при наличии нормального новостного события; обязательно сохрани атрибуцию («заявило Минобороны РФ», «сообщили в ведомстве» и т.п.).

Если нет:
- publish=false;
- объясни причину в reason.

ФОРМАТ ПУБЛИКАЦИИ:
- Заголовок: одна строка, без эмодзи.
- Текст: 2–3 коротких абзаца, ориентир 450–700 знаков, максимум 800.
- Только главное; не пересказывай второстепенные детали.
- Ключевые цифры можно выделить **жирным**.

Материалы источников:

{joined}
"""

    async def classify_update(self, previous_title, previous_text, new_source):
        """Decide whether a new report contains a materially important update."""
        prompt = f"""
Ты очень строго проверяешь новое сообщение для уже опубликованной новости.

СТАРАЯ ПУБЛИКАЦИЯ:
ЗАГОЛОВОК: {previous_title}
ТЕКСТ: {previous_text}

НОВОЕ СООБЩЕНИЕ:
ИСТОЧНИК: {new_source.get('source', 'unknown')}
ТЕКСТ: {new_source.get('text', '')}

Главная задача: определить, есть ли здесь ОСНОВАНИЕ ДЛЯ ОТДЕЛЬНОЙ
НОВОЙ ПУБЛИКАЦИИ-ОБНОВЛЕНИЯ.

ОЧЕНЬ ВАЖНО:
Если новое сообщение просто подробнее описывает то же событие,
повторяет уже известные факты, добавляет второстепенные детали,
описания обстановки, хронологию, цитаты или последствия, которые
не меняют статус события  это НЕ UPDATE.

НЕ ВАЖНЫЙ UPDATE:
- перефразировка старой новости;
- повтор уже известных фактов;
- более подробное описание уже известного события;
- новые несущественные детали;
- дополнительные сведения, которые не меняют масштаб или статус;
- повторное сообщение другого источника о том же самом;
- детали, которые логично следуют из уже опубликованной информации.

ВАЖНЫЙ UPDATE возможен только при появлении хотя бы одного
действительно существенного нового обстоятельства:
- заметно изменилось число погибших/пострадавших;
- существенно изменился масштаб события;
- произошло новое важное действие или последствие;
- изменился статус события;
- появилось официальное подтверждение или опровержение
  ключевого ранее спорного факта;
- появилась новая информация, которая существенно меняет
  понимание события.

ПРАВИЛО СТРОГОСТИ:
Если сомневаешься между UPDATE и повтором  выбирай НЕ UPDATE.

Оцени существенность по шкале ОТ 0 ДО 100:
030    обычный повтор / несущественная деталь;
3169   есть новая информация, но недостаточная для отдельной публикации;
7089   существенный UPDATE;
90100  критически важное изменение события.

Поле important=true разрешено только если score >= 70.

Верни только JSON:
{{
  "important": true,
  "score": 0,
  "reason": "кратко объясни, какой именно новый существенный факт появился"
}}
"""

        global LAST_REQUEST_AT
        async with LOCK:
            now = time.monotonic()
            wait = LLM_MIN_INTERVAL - (now - LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)

            providers = []
            if getattr(self.s, "groq_key", None):
                providers.append((
                    "groq",
                    self.s.groq_key,
                    getattr(self.s, "groq_model", "qwen/qwen3.8-27b"),
                    getattr(self.s, "groq_max_tokens", 1800),
                    getattr(self.s, "groq_temperature", 0.35),
                ))

            if getattr(self.s, "mistral_key", None):
                providers.append((
                    "mistral",
                    self.s.mistral_key,
                    getattr(self.s, "mistral_model", "mistral-small-latest"),
                    getattr(self.s, "mistral_max_tokens", 1800),
                    getattr(self.s, "mistral_temperature", 0.35),
                ))

            last_error = None

            for name, key, model, max_tokens, temperature in providers:
                try:
                    LAST_REQUEST_AT = time.monotonic()

                    if name == "groq":
                        raw = await asyncio.to_thread(
                            self._call_groq,
                            key,
                            model,
                            max_tokens,
                            temperature,
                            prompt,
                        )
                    else:
                        raw = await asyncio.to_thread(
                            self._call_mistral,
                            key,
                            model,
                            max_tokens,
                            temperature,
                            prompt,
                        )

                    data = self._parse(raw)

                    important = data.get("important", False)

                    if isinstance(important, str):
                        important = important.strip().lower() in {
                            "true", "1", "yes", "да"
                        }

                    try:
                        score = float(data.get("score", 0))
                    except (TypeError, ValueError):
                        score = 0.0

                    score = max(0.0, min(100.0, score))

                    result = {
                        "important": bool(important) and score >= 70.0,
                        "score": score,
                        "reason": str(
                            data.get("reason", "")
                        ).strip(),
                    }

                    log.info(
                        "UPDATE check provider=%s important=%s "
                        "score=%.0f reason=%s",
                        name,
                        result["important"],
                        result["score"],
                        result["reason"][:160],
                    )

                    return result

                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "LLM update check %s failed: %s",
                        name,
                        exc,
                    )

        if last_error:
            log.warning(
                "UPDATE check unavailable: %s",
                last_error,
            )

        return {
            "important": False,
            "score": 0.0,
            "reason": "update_check_failed",
        }

    async def compose_event_update(
        self,
        previous_title,
        previous_text,
        new_source,
    ):
        """Create a new update containing only verified new facts."""
        prompt = f"""
Ты выпускающий редактор новостного канала.

УЖЕ ОПУБЛИКОВАНО:

ЗАГОЛОВОК:
{previous_title}

ТЕКСТ:
{previous_text}

НОВЫЙ ИСТОЧНИК:

ИСТОЧНИК:
{new_source.get('source', 'unknown')}

ТЕКСТ:
{new_source.get('text', '')}

ЗАДАЧА:

Подготовь отдельную публикацию-обновление.

В неё должны попасть ТОЛЬКО существенные НОВЫЕ ФАКТЫ,
которых нет в старой публикации.

Не пересказывай старую новость заново.

СТРОГО ЗАПРЕЩЕНО:
- придумывать факты;
- использовать знания из собственной памяти;
- добавлять сведения, отсутствующие в материалах;
- придумывать цифры, имена, даты, места или последствия;
- превращать предположение в факт;
- усиливать формулировку источника;
- добавлять фантастические или абсурдные утверждения;
- выдавать заявление источника за независимо подтверждённый факт.

Если это заявление ведомства или должностного лица,
сохрани атрибуцию:
сообщило Минобороны..., заявил министр...,
по словам..., по данным....

Не включай в публикацию детали, которые уже содержатся
в старой новости, даже если они сформулированы иначе.

Если нового существенного факта нет:
publish=false.

Если новый источник содержит очевидную выдумку или абсурд:
publish=false.

ЗАГОЛОВОК должен описывать именно новое обстоятельство.

ТЕКСТ:
1–2 коротких абзаца;
только новые факты;
максимум 500 знаков;
без эмодзи и кликбейта.

Верни ТОЛЬКО JSON:

{{
  "publish": true,
  "title": "...",
  "text": "...",
  "confidence": 0.0,
  "reason": "какой новый факт добавлен"
}}

При отсутствии надёжного существенного нового факта:

{{
  "publish": false,
  "title": "",
  "text": "",
  "confidence": 0.0,
  "reason": "нового подтверждаемого существенного факта недостаточно"
}}
"""

        global LAST_REQUEST_AT

        async with LOCK:
            now = time.monotonic()
            wait = LLM_MIN_INTERVAL - (now - LAST_REQUEST_AT)

            if wait > 0:
                await asyncio.sleep(wait)

            providers = []

            if getattr(self.s, "groq_key", None):
                providers.append((
                    "groq",
                    self.s.groq_key,
                    getattr(self.s, "groq_model", "qwen/qwen3.8-27b"),
                    getattr(self.s, "groq_max_tokens", 900),
                    getattr(self.s, "groq_temperature", 0.2),
                ))

            if getattr(self.s, "mistral_key", None):
                providers.append((
                    "mistral",
                    self.s.mistral_key,
                    getattr(self.s, "mistral_model", "mistral-small-latest"),
                    getattr(self.s, "mistral_max_tokens", 900),
                    getattr(self.s, "mistral_temperature", 0.2),
                ))

            last_error = None

            for name, key, model, max_tokens, temperature in providers:
                try:
                    LAST_REQUEST_AT = time.monotonic()

                    if name == "groq":
                        raw = await asyncio.to_thread(
                            self._call_groq,
                            key,
                            model,
                            max_tokens,
                            temperature,
                            prompt,
                        )
                    else:
                        raw = await asyncio.to_thread(
                            self._call_mistral,
                            key,
                            model,
                            max_tokens,
                            temperature,
                            prompt,
                        )

                    data = self._parse(raw)

                    publish = data.get("publish", False)

                    if isinstance(publish, str):
                        publish = publish.strip().lower() in {
                            "true", "1", "yes", "да"
                        }

                    try:
                        confidence = float(data.get("confidence", 0))
                    except (TypeError, ValueError):
                        confidence = 0.0

                    confidence = max(0.0, min(1.0, confidence))

                    title = str(data.get("title", "")).strip()
                    text = str(data.get("text", "")).strip()
                    reason = str(data.get("reason", "")).strip()

                    if not title or not text:
                        publish = False

                    max_length = getattr(
                        self.s,
                        "max_post_length",
                        500,
                    )

                    if len(text) > max_length:
                        text = (
                            text[:max_length - 3]
                            .rstrip()
                            + "..."
                        )

                    result = {
                        "publish": bool(publish),
                        "title": title,
                        "text": text,
                        "confidence": confidence,
                        "reason": reason,
                    }

                    log.info(
                        "UPDATE compose provider=%s publish=%s confidence=%.2f reason=%s",
                        name,
                        result["publish"],
                        confidence,
                        reason[:160],
                    )

                    return result

                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "LLM update compose %s failed: %s",
                        name,
                        exc,
                    )

        return {
            "publish": False,
            "title": "",
            "text": "",
            "confidence": 0.0,
            "reason": f"update_compose_failed: {last_error}",
        }

    # ---------------------------------------------------------
    # Основной метод
    # ---------------------------------------------------------

    async def edit(self, sources):
        if not sources:
            raise ValueError(
                "LLM.edit() received no sources"
            )

        prompt = self._build_prompt(sources)

        global LAST_REQUEST_AT

        async with LOCK:
            # Groq can return HTTP 429 during startup backfill if requests
            # are sent back-to-back. Keep one global request cadence for the
            # whole process. The lock also prevents concurrent LLM calls.
            now = time.monotonic()
            wait = LLM_MIN_INTERVAL - (now - LAST_REQUEST_AT)
            if wait > 0:
                log.info("LLM rate limit: sleeping %.1fs before next request", wait)
                await asyncio.sleep(wait)

            providers = []

            if getattr(self.s, "groq_key", None):
                providers.append(
                    (
                        "groq",
                        self.s.groq_key,
                        getattr(
                            self.s,
                            "groq_model",
                            "qwen/qwen3.8-27b",
                        ),
                        getattr(
                            self.s,
                            "groq_max_tokens",
                            1800,
                        ),
                        getattr(
                            self.s,
                            "groq_temperature",
                            0.35,
                        ),
                    )
                )

            if getattr(self.s, "mistral_key", None):
                providers.append(
                    (
                        "mistral",
                        self.s.mistral_key,
                        getattr(
                            self.s,
                            "mistral_model",
                            "mistral-small-latest",
                        ),
                        getattr(
                            self.s,
                            "mistral_max_tokens",
                            1800,
                        ),
                        getattr(
                            self.s,
                            "mistral_temperature",
                            0.35,
                        ),
                    )
                )

            if not providers:
                raise RuntimeError(
                    "No LLM API key configured: "
                    "set GROQ_API_KEY or MISTRAL_API_KEY"
                )

            last_error = None

            for (
                name,
                key,
                model,
                max_tokens,
                temperature,
            ) in providers:

                try:
                    log.info(
                        "LLM request provider=%s model=%s",
                        name,
                        model,
                    )

                    LAST_REQUEST_AT = time.monotonic()

                    if name == "groq":
                        raw = await asyncio.to_thread(
                            self._call_groq,
                            key,
                            model,
                            max_tokens,
                            temperature,
                            prompt,
                        )
                    else:
                        raw = await asyncio.to_thread(
                            self._call_mistral,
                            key,
                            model,
                            max_tokens,
                            temperature,
                            prompt,
                        )

                    data = self._normalize(self._parse(raw))

                    # Нормализация.
                    data["publish"] = bool(
                        data.get("publish", False)
                    )

                    data["title"] = str(
                        data.get("title", "")
                    ).strip()

                    data["text"] = str(
                        data.get("text", "")
                    ).strip()

                    data["category"] = str(
                        data.get("category", "other")
                    ).strip().lower()

                    if data["category"] not in {
                        "politics",
                        "svo",
                        "russia",
                        "world",
                        "economy",
                        "security",
                        "other",
                    }:
                        data["category"] = "other"

                    try:
                        data["priority"] = int(
                            data.get("priority", 0)
                        )
                    except (TypeError, ValueError):
                        data["priority"] = 0

                    data["priority"] = max(
                        0,
                        min(100, data["priority"]),
                    )

                    try:
                        data["confidence"] = float(
                            data.get("confidence", 0)
                        )
                    except (TypeError, ValueError):
                        data["confidence"] = 0.0

                    data["confidence"] = max(
                        0.0,
                        min(1.0, data["confidence"]),
                    )

                    data["reason"] = str(
                        data.get("reason", "")
                    ).strip()

                    max_length = getattr(
                        self.s,
                        "max_post_length",
                        500,
                    )

                    if len(data["text"]) > max_length:
                        data["text"] = (
                            data["text"][
                                : max_length - 3
                            ].rstrip()
                            + "..."
                        )

                    # Защита от явно неполного результата.
                    if not data["text"]:
                        data["publish"] = False
                        data["reason"] = (
                            data["reason"]
                            or "LLM не сформировал текст новости"
                        )

                    if not data["title"]:
                        data["publish"] = False
                        data["reason"] = (
                            data["reason"]
                            or "LLM не сформировал заголовок"
                        )

                    log.info(
                        "LLM provider=%s category=%s "
                        "priority=%s confidence=%.2f publish=%s",
                        name,
                        data["category"],
                        data["priority"],
                        data["confidence"],
                        data["publish"],
                    )

                    return data

                except Exception as exc:
                    last_error = exc

                    log.warning(
                        "LLM %s failed: %s",
                        name,
                        exc,
                    )

            raise RuntimeError(
                f"All LLM providers failed: {last_error}"
            )