import asyncio
import json
import logging
import re
import time

import requests

from .router import (
    request as llm_request,
    groq_available,
)



log = logging.getLogger("llm")
LOCK = asyncio.Lock()
LLM_MIN_INTERVAL = 4.0
LAST_REQUEST_AT = 0.0


SYSTEM = """
Ты редактор новостей на русском языке.

ТЕБЕ ПЕРЕДАЮТ НОВОСТНОЙ МАТЕРИАЛ.
Твоя задача  обработать ТОЛЬКО этот материал и вернуть результат в JSON.

КРИТИЧЕСКОЕ ПРАВИЛО:
Используй только факты из переданного материала.
Не используй интернет, внешние источники, свои знания или память.
Не ищи подтверждения в интернете.
Не добавляй сведения, которых нет во входном материале.

ЗАПРЕЩЕНО:
- придумывать факты;
- добавлять имена, даты, места, цифры и обстоятельства;
- добавлять причины или последствия, которых нет в материале;
- исправлять или дополнять материал на основании собственных знаний;
- ссылаться на внешние источники;
- писать текст до JSON или после JSON;
- использовать markdown;
- заключать JSON в ```.

ОПРЕДЕЛЕНИЕ НОВОСТИ:

publish=true, если во входном материале описано конкретное событие или конкретный факт, который можно кратко пересказать.

publish=false, если:
- во входном материале нет события;
- материала недостаточно для самостоятельного новостного сообщения;
- это реклама;
- это поздравление;
- это мнение без конкретного события;
- это слух или предположение без указания на факт;
- это флуд;
- входной текст фактически отсутствует.

ВАЖНО:
Краткое сообщение всё равно может быть новостью.

Например, если материал сообщает:
"ТАСС: Российские средства ПВО за ночь уничтожили несколько беспилотников над регионами России. Точное количество и регионы не уточняются."

это достаточно для publish=true.

В таком случае нельзя придумывать количество БПЛА, регионы, типы беспилотников или другие детали.

АТРИБУЦИЯ:

Если во входном материале написано, что информацию сообщает конкретное СМИ, ведомство или должностное лицо, сохрани эту атрибуцию.

Например:
"ТАСС сообщает, что..."
"Минобороны России заявило, что..."

Не превращай заявление источника в самостоятельно установленный факт.

РЕРАЙТ:

Для publish=true:
- title  короткий заголовок;
- text  самостоятельный короткий новостной текст;
- не повторяй заголовок дословно первым предложением;
- сохраняй только существенные факты;
- не растягивай текст;
- не добавляй вступления вроде "стало известно", если они не нужны;
- не добавляй факты от себя.

Для publish=false:
- title должен быть "";
- text должен быть "";
- priority должен быть 0;
- confidence должен отражать уверенность именно в решении publish=false.

КАТЕГОРИЯ:

Используй только:
politics
svo
russia
world
economy
security
other

Если материал невозможно уверенно отнести к одной из основных категорий  используй other.

PRIORITY:

Число от 0 до 10.

02  обычная новость.
34  заметное событие.
56  важное событие.
78  очень важное событие.
910  критически важное событие.

Не повышай priority только потому, что событие эмоционально сформулировано.

CONFIDENCE:

Число от 0.0 до 1.0.

Это уверенность в правильности решения и классификации на основании ТОЛЬКО входного материала.

Если фактов мало, confidence не должен автоматически быть 1.0.

HASHTAGS:

Верни РОВНО 3 хештега.

Каждый хештег:
- начинается с #;
- связан непосредственно с новостью;
- короткий;
- уникальный;
- без эмодзи.

Не используй:
#Новости
#Срочно
#Важно
#Главное
#События

Если publish=false, всё равно верни ровно 3 хештега, связанные с причиной или тематикой входного материала.

ФОРМАТ:

Верни строго один JSON-объект со следующими полями:

{
  "publish": true,
  "title": "...",
  "text": "...",
  "category": "politics|svo|russia|world|economy|security|other",
  "priority": 0,
  "confidence": 0.0,
  "reason": "...",
  "hashtags": ["#...", "#...", "#..."]
}

ТРЕБОВАНИЯ К JSON:

- только двойные кавычки;
- никаких комментариев;
- никаких дополнительных полей;
- publish  только true или false;
- priority  число от 0 до 10;
- confidence  число от 0.0 до 1.0;
- hashtags  массив ровно из 3 строк;
- JSON должен быть синтаксически корректным.

ПЕРЕД ОТВЕТОМ ПРОВЕРЬ:

1. Все факты в title есть во входном материале.
2. Все факты в text есть во входном материале.
3. Ничего не добавлено из внешних знаний.
4. category допустима.
5. priority находится от 0 до 10.
6. confidence находится от 0.0 до 1.0.
7. hashtags содержит ровно 3 элемента.
8. Ответ содержит ТОЛЬКО JSON.
"""



class LLM:
    def __init__(self, s):
        self.s = s

    # ---------------------------------------------------------
    # Groq через официальный Python SDK
    # ---------------------------------------------------------

    def _call_groq(
        self,
        key,
        model,
        max_tokens,
        temperature,
        prompt,
    ):
        return llm_request(
            provider="groq",
            key=key,
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.s.llm_timeout,
            min_interval=getattr(
                self.s,
                "llm_min_interval_seconds",
                LLM_MIN_INTERVAL,
            ),
            groq_cooldown=getattr(
                self.s,
                "groq_cooldown_seconds",
                30,
            ),
            extra={
                "reasoning_effort": "none",
                "reasoning_format": "hidden",
                "response_format": {"type": "json_object"},
            },
        )

    # ---------------------------------------------------------
    # Mistral fallback
    # ---------------------------------------------------------

    # ---------------------------------------------------------
    # GigaChat fallback
    # ---------------------------------------------------------

    def _call_gigachat(
        self,
        key,
        model,
        max_tokens,
        temperature,
        prompt,
    ):
        return llm_request(
            provider="gigachat",
            key=key,
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.s.llm_timeout,
            min_interval=getattr(
                self.s,
                "llm_min_interval_seconds",
                LLM_MIN_INTERVAL,
            ),
            groq_cooldown=getattr(
                self.s,
                "groq_cooldown_seconds",
                30,
            ),
        )

    def _call_mistral(
        self,
        key,
        model,
        max_tokens,
        temperature,
        prompt,
    ):
        return llm_request(
            provider="mistral",
            key=key,
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.s.llm_timeout,
            min_interval=getattr(
                self.s,
                "llm_min_interval_seconds",
                LLM_MIN_INTERVAL,
            ),
            groq_cooldown=getattr(
                self.s,
                "groq_cooldown_seconds",
                30,
            ),
        )

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
        max_length = getattr(self.s, "max_post_length", 450)

        if len(data["text"]) > max_length:
            text = data["text"].strip()

            # Сначала пытаемся закончить материал на границе предложения.
            cut = -1
            for marker in (". ", "! ", "? ", ".\\n", "!\\n", "?\\n"):
                pos = text.rfind(marker, 0, max_length + 1)
                if pos > cut:
                    cut = pos + 1

            # Не оставляем слишком короткий обрубок.
            if cut >= 250:
                text = text[:cut].rstrip()
            else:
                # Крайний случай: режем хотя бы по целому слову.
                text = text[:max_length].rsplit(" ", 1)[0].rstrip()

            data["text"] = text

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
            wait = float(getattr(self.s, "llm_min_interval_seconds", LLM_MIN_INTERVAL)) - (now - LAST_REQUEST_AT)
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

            if getattr(self.s, "gigachat_key", None):
                providers.append((
                    "gigachat",
                    self.s.gigachat_key,
                    getattr(self.s, "gigachat_model", "GigaChat-2"),
                    getattr(self.s, "gigachat_max_tokens", 600),
                    getattr(self.s, "gigachat_temperature", 0.35),
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
                # Если Groq уже на cooldown  даже не пытаемся его вызвать.
                if name == "groq" and not groq_available():
                    log.info(
                        "LLM provider=groq cooldown active; using next provider"
                    )
                    continue

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
                    elif name == "gigachat":
                        raw = await asyncio.to_thread(
                            self._call_gigachat,
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
            wait = float(getattr(self.s, "llm_min_interval_seconds", LLM_MIN_INTERVAL)) - (now - LAST_REQUEST_AT)

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

            if getattr(self.s, "gigachat_key", None):
                providers.append((
                    "gigachat",
                    self.s.gigachat_key,
                    getattr(self.s, "gigachat_model", "GigaChat-2"),
                    getattr(self.s, "gigachat_max_tokens", 600),
                    getattr(self.s, "gigachat_temperature", 0.35),
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
                # Если Groq уже на cooldown  даже не пытаемся его вызвать.
                if name == "groq" and not groq_available():
                    log.info(
                        "LLM provider=groq cooldown active; using next provider"
                    )
                    continue

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
                    elif name == "gigachat":
                        raw = await asyncio.to_thread(
                            self._call_gigachat,
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
            wait = float(getattr(self.s, "llm_min_interval_seconds", LLM_MIN_INTERVAL)) - (now - LAST_REQUEST_AT)
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

            if getattr(self.s, "gigachat_key", None):
                providers.append((
                    "gigachat",
                    self.s.gigachat_key,
                    getattr(self.s, "gigachat_model", "GigaChat-2"),
                    getattr(self.s, "gigachat_max_tokens", 600),
                    getattr(self.s, "gigachat_temperature", 0.35),
                ))

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

                # Groq уже на cooldown  не вызываем API и
                # даже не логируем фиктивный request. Сразу Mistral.
                if name == "groq" and not groq_available():
                    log.info(
                        "LLM provider=groq cooldown active; using next provider"
                    )
                    continue

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
                    elif name == "gigachat":
                        raw = await asyncio.to_thread(
                            self._call_gigachat,
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

                    # Нормализация хештегов.
                    raw_hashtags = data.get("hashtags", [])

                    if isinstance(raw_hashtags, str):
                        raw_hashtags = raw_hashtags.replace(",", " ").split()

                    hashtags = []
                    seen = set()

                    if isinstance(raw_hashtags, list):
                        for tag in raw_hashtags:
                            tag = str(tag).strip().lstrip("#")
                            tag = re.sub(
                                r"[^\wА-Яа-яЁё0-9_]",
                                "",
                                tag,
                            )

                            if not tag:
                                continue

                            tag = "#" + tag
                            key = tag.lower()

                            if key not in seen:
                                seen.add(key)
                                hashtags.append(tag)

                    data["hashtags"] = hashtags[:3]

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

                    fallback_tags = {
                        "politics": "#Политика",
                        "svo": "#СВО",
                        "russia": "#Россия",
                        "world": "#Мир",
                        "economy": "#Экономика",
                        "security": "#Безопасность",
                        "other": "#События",
                    }

                    fallback = fallback_tags[data["category"]]

                    if fallback.lower() not in {
                        x.lower() for x in data["hashtags"]
                    }:
                        data["hashtags"].append(fallback)

                    for tag in ("#Главное", "#События"):
                        if len(data["hashtags"]) >= 3:
                            break

                        if tag.lower() not in {
                            x.lower() for x in data["hashtags"]
                        }:
                            data["hashtags"].append(tag)

                    data["hashtags"] = data["hashtags"][:3]

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

                    # Хештеги отдельной строкой внизу.
                    data["text"] = (
                        data["text"].rstrip()
                        + "\n\n"
                        + " ".join(data["hashtags"])
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
