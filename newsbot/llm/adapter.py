import asyncio
import json
import logging
import re
import time

import requests

from .router import (
    request as llm_request,
    groq_available,
    provider_available,
    credential_available,
    mark_provider_cooldown,
    mark_provider_transient_failure,
    clear_provider_cooldown,
    clear_credential_cooldown,
    mark_credential_cooldown,
    TRANSIENT_PROVIDER_COOLDOWN,
    TRANSIENT_CREDENTIAL_COOLDOWN,
    AUTH_CREDENTIAL_COOLDOWN,
)



log = logging.getLogger("llm")
LOCK = asyncio.Lock()
LLM_MIN_INTERVAL = 4.0
LAST_REQUEST_AT = 0.0


CORE_SYSTEM = """
Ты обрабатываешь только переданный материал.
Не используй внешние знания, интернет или догадки; не добавляй факты.
Сохраняй атрибуцию заявлений.
Верни только требуемый JSON без markdown и служебного текста.
"""

DIGEST_SYSTEM = CORE_SYSTEM + """
Задача: определить, есть ли в материале несколько независимых новостных сюжетов.
Связанные реакции, подтверждения, последствия и развитие одного повода не являются отдельными сюжетами.
Ставь true только при наличии второго самостоятельного news hook.
"""

UPDATE_CHECK_SYSTEM = CORE_SYSTEM + """
Задача: строго оценить, появился ли существенный новый факт по уже известному событию.
Не считай UPDATE перефразировкой, повтором известного факта, мелкой деталью, очевидным следствием, мнением или прогнозом.
Новый результат, решение, действие, объект, участник или изменение статуса может быть существенным.
"""

UPDATE_COMPOSE_SYSTEM = CORE_SYSTEM + """
Задача: сформировать короткий UPDATE только из существенного нового факта, отсутствующего в старой публикации.
Не пересказывай старую публикацию и не добавляй выводов, причин, последствий или аналитики.
"""

COMPACT_SYSTEM = CORE_SYSTEM + """
Задача: семантически сократить уже подготовленный новостной текст до указанного лимита знаков.
Сохрани главный факт, существенные цифры, имена, места и атрибуцию; удали воду и повторы.
Не добавляй факты и не обрезай текст механически.
"""


THREADS_SYSTEM = CORE_SYSTEM + """
Ты адаптируешь уже полностью подготовленную и одобренную новостную публикацию для Threads.

Это НЕ редактура исходного материала и НЕ проверка новости.
Все проверки, дедупликация, определение NEW/UPDATE, confidence и решение о публикации уже выполнены.

Твоя задача:
- семантически переписать публикацию специально для Threads;
- уложить результат в указанный лимит знаков;
- сохранить главный новостной факт;
- сохранить существенные цифры, имена, места и атрибуцию;
- убрать воду, повторы и второстепенные детали;
- не добавлять факты, выводы, причины или последствия;
- не использовать внешние знания;
- не обрезать текст механически;
- вернуть законченный, самостоятельный и грамматически полноценный текст.

Верни только JSON без markdown:
{"text": "..."}
"""

SYSTEM = """
Ты редактор новостей на русском языке.

ТЕБЕ ПЕРЕДАЮТ НОВОСТНОЙ МАТЕРИАЛ.
Твоя задача  обработать ТОЛЬКО этот материал и вернуть результат в JSON.

КРИТИЧЕСКОЕ ПРАВИЛО:
Используй только факты из переданного материала.
Не используй интернет, внешние источники, свои знания или память.
Не ищи подтверждения в интернете.
Не добавляй сведения, которых нет во входном материале.

ФАКТИЧЕСКАЯ ОДНОЗНАЧНОСТЬ:
- Не объединяй в одном предложении факты, если такое объединение создаёт неоднозначное или физически противоречивое описание расположения людей, предметов или их взаимного положения.
- Не додумывай, где находится человек или предмет, кто что держит, где находится человек относительно объекта и другие пространственные отношения, если это прямо не указано во входном материале.
- Если исходный материал неоднозначен, перестрой предложение нейтрально, сохранив только подтверждённые факты.
- Не создавай из двух отдельных фактов новую связь между ними только ради более плавного текста.

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

СТРОГИЙ РЕЖИМ РАБОТЫ С ВХОДОМ:

- Работай ТОЛЬКО с текстом, переданным в блоке материала.
- Не отвечай на вопросы пользователя, если их нет в материале.
- Не объясняй ограничения модели, не выражай собственное мнение и не добавляй рассуждения.
- Не используй знания модели для дополнения, проверки или исправления фактов.
- Если входной материал относится к чувствительной теме, это само по себе НЕ является причиной придумывать ответ, менять смысл материала или добавлять предупреждения.
- Если модель не может безопасно или корректно выполнить редакционную задачу, верни publish=false в требуемом JSON-формате.
- При publish=false обязательно сохраняй строгий JSON-контракт; никогда не заменяй JSON служебным текстом.
- Никогда не возвращай фразы вроде "я не могу", "как языковая модель", "я не обладаю собственным мнением", "ответ ограничен" или объяснения политики вместо JSON.

publish=false, если:
- во входном материале нет события;
- материала недостаточно для самостоятельного новостного сообщения;
- это реклама;
- это поздравление;
- это мнение без конкретного события;
- это аналитика, прогноз, разбор или экспертная оценка;
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

    @staticmethod
    def _is_non_news_text(text):
        value = " ".join(str(text or "").lower().split())
        markers = (
            "дайджест", "различные новости", "главные новости дня",
            "главные новости за", "обзор новостей", "итоги дня",
            "итоги недели", "аналитика", "аналитический разбор",
            "аналитический материал", "экспертный анализ", "что это значит",
            "почему это важно", "прогноз", "перспективы развития",
        )
        return any(marker in value for marker in markers)

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

    def _call_gemini(
        self,
        key,
        model,
        max_tokens,
        temperature,
        prompt,
        system_prompt=None,
    ):
        return llm_request(
            provider="gemini",
            key=key,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt or SYSTEM,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
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
        if not isinstance(data, dict):
            raise ValueError("LLM JSON must be an object")
        publish = data.get("publish", False)
        if isinstance(publish, str):
            publish = publish.strip().lower() in {"true", "1", "yes", "да"}
        title = str(data.get("title", "")).strip()
        body = str(data.get("text", "")).strip()
        category = str(data.get("category", "other")).strip().lower()
        if category not in {"politics", "svo", "russia", "world", "economy", "security", "other"}:
            category = "other"
        try:
            priority = max(0, min(10, int(data.get("priority", 0))))
        except (TypeError, ValueError):
            priority = 0
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason", "")).strip()
        is_digest = data.get("is_digest", False)
        if isinstance(is_digest, str):
            is_digest = is_digest.strip().lower() in {"true", "1", "yes", "да"}
        if is_digest:
            publish = False
            title = ""
            body = ""
            reason = reason or "material is a digest"
        if not title or not body:
            publish = False
            reason = reason or "LLM не сформировал полный материал"
        return {
            **data,
            "publish": bool(publish),
            "title": title,
            "text": body,
            "category": category,
            "priority": priority,
            "confidence": confidence,
            "reason": reason,
            "is_digest": bool(is_digest),
        }

    async def _check_digest(self, sources):
        if not sources:
            return False, ""

        source_text = "\n\n".join(
            str(source.get("text", "")).strip()
            for source in sources
        )

        title = source_text.split("\n", 1)[0].strip()
        body = source_text.split("\n", 1)[1].strip() if "\n" in source_text else ""

        prompt = f"""
Ты второй редактор-контролёр.

Определи, является ли материал ДАЙДЖЕСТОМ.

Главный принцип:
если заголовок и начало материала задают ОДИН главный информационный повод,
то заявления, реакции, решения, отмены рейсов и действия других участников,
которые объясняют, подтверждают, развивают или являются последствиями этой же
ситуации, НЕ являются отдельными сюжетами.

Ставь is_digest=true ТОЛЬКО если в материале есть второй самостоятельный
информационный повод, который не является контекстом, реакцией, подтверждением,
последствием или развитием главного повода.

Не считай отдельным сюжетом просто факт, который можно отдельно пересказать.
Нужно наличие отдельного события с собственным независимым news hook.

ЗАГОЛОВОК:
{title}

ТЕКСТ:
{body}

Верни только JSON:
{{"is_digest": false, "reason": ""}}
"""

        raw = await self._request_with_fallback(
            prompt,
            task="digest_adjudication",
            max_tokens=300,
            system_prompt=DIGEST_SYSTEM,
            temperature=0.0,
        )

        if raw is None:
            return None, "digest_adjudication_unavailable"

        try:
            data = self._parse(raw)
        except Exception as exc:
            log.warning("LLM digest adjudication parse failed: %s", exc)
            return None, "digest_adjudication_parse_failed"

        value = data.get("is_digest", False)
        if isinstance(value, str):
            value = value.strip().lower() in {"true", "1", "yes", "да"}

        return bool(value), str(data.get("reason", "")).strip()

    def _build_prompt(self, sources):
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

СНАЧАЛА ПРОВЕРЬ, НЕ ЯВЛЯЕТСЯ ЛИ МАТЕРИАЛ ДАЙДЖЕСТОМ.

ДАЙДЖЕСТ  это материал, в котором собраны ДВА ИЛИ БОЛЕЕ
САМОСТОЯТЕЛЬНЫХ НОВОСТНЫХ СЮЖЕТА, каждый из которых можно
опубликовать как отдельную новость без потери смысла.

КЛЮЧЕВОЕ ПРАВИЛО:
Считай НЕ количество фактов, предложений, цифр или деталей,
а количество самостоятельных событий/сюжетов.

НЕ ЯВЛЯЕТСЯ ДАЙДЖЕСТОМ, если:
- несколько фактов описывают ОДНО событие;
- перечислены детали одного происшествия;
- приведены последствия, причины, участники или результаты одного события;
- приведены несколько этапов или обновлений одного продолжающегося эпизода;
- один человек/организация фигурирует в одном конкретном деле, а остальные сведения являются его контекстом или предысторией;
- одно заявление содержит несколько связанных тезисов или деталей;
- одно решение/распоряжение/NOTAM сопровождается несколькими связанными последствиями;
- несколько объектов упомянуты как детали одной операции, проверки, расследования или инцидента.

ОСОБЫЙ СЛУЧАЙ  ОДНА ОБЩАЯ СИТУАЦИЯ:
Если материал имеет один главный информационный повод и описывает одну общую развивающуюся ситуацию, не считай его дайджестом только потому, что в нём упомянуты действия или заявления нескольких участников.
Связанные действия разных организаций, заявления властей, решения других участников, отмены/изменения услуг и последствия могут быть частью одного сюжета, если они относятся к тому же общему вопросу и помогают раскрыть главный повод материала.
ОСОБЕННО ВАЖНО: наличие нескольких самостоятельных действий разных организаций ещё НЕ означает несколько сюжетов. Если эти действия являются реакциями, подтверждениями, аналогичными мерами или последствиями одной общей ситуации, считай их одним сюжетом.
СНАЧАЛА ОПРЕДЕЛИ ГЛАВНЫЙ NEWS HOOK ПО ЗАГОЛОВКУ И ПЕРВОМУ АБЗАЦУ. Если материал построен вокруг одного конкретного события или действия, именно оно определяет основной сюжет. Последующие заявления, решения, отмены, реакции или действия других участников по той же ситуации по умолчанию являются контекстом или развитием основного сюжета, а НЕ отдельным сюжетом.
Не превращай материал в дайджест только потому, что отдельный абзац можно теоретически превратить в самостоятельную короткую новость. Для digest нужно доказать наличие второго независимого информационного повода, а не просто возможность выделить из текста отдельный факт.

Разделяй материал на несколько сюжетов только тогда, когда второй сюжет имеет собственный независимый информационный повод, который не является реакцией, подтверждением, последствием или развитием главного события.
Проверяй не возможность физически разделить абзацы, а наличие действительно разных самостоятельных информационных поводов.

Примеры НЕ-дайджеста:
- одно ДТП + погибшие + пострадавшие + место + причины;
- одно уголовное дело + задержание + обвинение + прежние действия того же лица;
- одна операция полиции + количество изъятого + задержанные + найденные лаборатории;
- один официальный документ + его содержание + связанные последствия;
- одно заявление чиновника + несколько связанных тезисов из этого заявления.

ДАЙДЖЕСТ  только если в материале действительно есть
НЕСКОЛЬКО НЕЗАВИСИМЫХ сюжетов, например:
- разные события с разными поводами;
- разные несвязанные заявления;
- разные встречи по разным вопросам;
- независимые новости по разным темам, которые можно разделить
  на отдельные публикации.

ПРОВЕРКА ПЕРЕД is_digest=true:
1. Выдели предполагаемые сюжеты.
2. Для каждого сюжета определи конкретный повод/эпизод.
3. Если сюжеты являются частями одного события или одного
   продолжающегося эпизода  is_digest=false.
4. Если это действительно независимые события, которые можно
   опубликовать отдельно  is_digest=true.

ВАЖНО:
НЕ ставь is_digest=true только потому, что в тексте много фактов,
цифр, предложений, участников, объектов или географических точек.
Несколько фактов одного события  это ОДНА новость.

Если это дайджест:
- is_digest=true;
- publish=false;
- не объединяй независимые сюжеты в одно искусственное событие;
- объясни причину в reason.

Если это один конкретный сюжет/эпизод:
- is_digest=false;
- продолжай обычную обработку новости.

АНАЛИТИКА ЗАПРЕЩЕНА:
Не публикуй анализ, прогноз, интерпретацию, оценку, объяснение
"почему это произошло" или "что это значит". Публикация должна
сообщать только конкретный факт/событие из исходного материала.


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
- Текст: ОДИН абзац, обычно 2-3 предложения.
- Не повторяй заголовок в тексте.
- Не начинай текст пересказом заголовка.
- Каждое предложение должно добавлять существенную новую информацию.
- Убирай воду, повторы и второстепенные детали.
- Не обрезай текст механически по количеству символов: сокращение должно быть семантическим.
- Только главный факт; не пересказывай второстепенные детали.
- Ключевые цифры можно выделить **жирным**.

ПОЛЕ is_digest ОБЯЗАТЕЛЬНО:
- true, если материал является дайджестом из нескольких самостоятельных сюжетов;
- false, если все факты относятся к одному событию или одному продолжающемуся эпизоду.

В JSON ОБЯЗАТЕЛЬНО верни поле:
"is_digest": true или false

Материалы источников:

{joined}
"""

    def _call_provider_with_system(self, name, key, model, tokens, temp, prompt, system_prompt):
        if not system_prompt:
            if name == "groq":
                return self._call_groq(key, model, tokens, temp, prompt)
            if name == "gigachat":
                return self._call_gigachat(key, model, tokens, temp, prompt)
            if name == "gemini":
                return self._call_gemini(key, model, tokens, temp, prompt)
            return self._call_mistral(key, model, tokens, temp, prompt)

        if name == "gemini":
            return self._call_gemini(
                key,
                model,
                tokens,
                temp,
                prompt,
                system_prompt=system_prompt,
            )
        return llm_request(
            provider=name, key=key, model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            max_tokens=tokens, temperature=temp, timeout=self.s.llm_timeout,
            min_interval=getattr(self.s, "llm_min_interval_seconds", LLM_MIN_INTERVAL),
            groq_cooldown=getattr(self.s, "groq_cooldown_seconds", 30),
            extra={"reasoning_effort": "none", "reasoning_format": "hidden",
                   "response_format": {"type": "json_object"}} if name == "groq" else None,
        )

    async def _request_with_fallback(self, prompt, task, max_tokens, temperature, system_prompt=None):
        global LAST_REQUEST_AT
        async with LOCK:
            now = time.monotonic()
            wait = float(getattr(self.s, "llm_min_interval_seconds", LLM_MIN_INTERVAL)) - (now - LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)
            provider_order = {
                "editorial": ("groq", "mistral", "gemini"),
                "editorial_single_story": ("groq", "mistral", "gemini"),
                "editorial_compact": ("mistral", "gemini"),
                "threads_compose": ("mistral", "gemini"),
                "digest_adjudication": ("gigachat", "mistral", "gemini"),
                "story_classify": ("mistral", "groq", "gemini"),
                "update_check": ("mistral", "groq", "gemini"),
                "update_compose": ("mistral", "gemini"),
            }

            names = provider_order.get(task, ("mistral", "groq"))
            providers = []

            for name in names:
                if name == "groq":
                    keys = getattr(self.s, "groq_keys", ()) or (
                        getattr(self.s, "groq_key", ""),
                    )
                    for key in keys:
                        if key:
                            providers.append((
                                "groq",
                                key,
                                getattr(self.s, "groq_model", "qwen/qwen3.8-27b"),
                                max_tokens,
                                temperature,
                            ))
                elif name == "mistral":
                    keys = getattr(self.s, "mistral_keys", ()) or (
                        getattr(self.s, "mistral_key", ""),
                    )
                    for key in keys:
                        if key:
                            providers.append((
                                "mistral",
                                key,
                                getattr(self.s, "mistral_model", "mistral-small-latest"),
                                max_tokens,
                                temperature,
                            ))
                elif name == "gigachat" and getattr(self.s, "gigachat_key", ""):
                    providers.append((
                        "gigachat",
                        self.s.gigachat_key,
                        getattr(self.s, "gigachat_model", "GigaChat-2"),
                        max_tokens,
                        temperature,
                    ))
                elif name == "gemini" and getattr(self.s, "gemini_key", ""):
                    providers.append((
                        "gemini",
                        self.s.gemini_key,
                        getattr(self.s, "gemini_model", "gemini-3.6-flash"),
                        max_tokens,
                        temperature,
                    ))
            last_error = None

            for name, key, model, tokens, temp in providers:
                if not credential_available(name, key):
                    log.info(
                        "LLM provider=%s credential cooldown active; using next credential",
                        name,
                    )
                    continue

                if name == "groq" and not groq_available(key):
                    log.info(
                        "LLM provider=groq credential cooldown active; using next credential"
                    )
                    continue

                try:
                    LAST_REQUEST_AT = time.monotonic()
                    raw = await asyncio.to_thread(
                        self._call_provider_with_system,
                        name, key, model, tokens, temp, prompt, system_prompt,
                    )
                    self._parse(raw)

                    clear_credential_cooldown(name, key)
                    clear_provider_cooldown(name)

                    return raw

                except Exception as exc:
                    last_error = exc
                    message = str(exc)
                    lower = message.lower()

                    if "cooldown active" not in lower:
                        log.warning(
                            "LLM %s %s failed: %s",
                            task,
                            name,
                            exc,
                        )

                    if (
                        "401" in lower
                        or "403" in lower
                        or "unauthorized" in lower
                    ):
                        mark_credential_cooldown(
                            name,
                            key,
                            AUTH_CREDENTIAL_COOLDOWN,
                        )

                    elif (
                        "429" in lower
                        or "timeout" in lower
                        or "timed out" in lower
                        or "connection" in lower
                        or "502" in lower
                        or "503" in lower
                        or "504" in lower
                        or "temporarily" in lower
                    ):
                        mark_credential_cooldown(
                            name,
                            key,
                            TRANSIENT_CREDENTIAL_COOLDOWN,
                        )


            log.warning("LLM %s unavailable: %s", task, last_error)
            return None

    async def classify_story(self, stories_context):
        """Semantic story classification. Returns relation/story/new facts/update text."""
        prompt = f"""
{stories_context}

Верни только JSON:
{{"relation":"new|duplicate|update|digest|unknown","story_id":0,"confidence":0.0,"new_facts":[],"update_text":"","reason":""}}
"""
        story_system = """Ты строгий редактор и классификатор конкретных новостных эпизодов.

Главное правило: STORY — это один конкретный реальный эпизод или его непосредственное развитие. STORY НЕ равна теме, персоне, стране, организации, войне, конфликту или общей повестке.

ИСТОЧНИК ИСТИНЫ:
- RAW_MESSAGES — канонический источник.
- FACTS — вторичный индекс и могут содержать ошибки.
- Редакторский draft — вторичен и не может исправить RAW.

КЛАССИФИКАЦИЯ:
- duplicate: тот же конкретный эпизод, но нового существенного факта нет. Перефразировка или другой источник с тем же фактом тоже duplicate.
- update: тот же конкретный эпизод И текущий RAW добавляет существенное новое действие, результат, решение, статус или иной новостной факт.
- new: один отдельный конкретный эпизод, для которого нет подходящей STORY.
- digest: материал содержит несколько самостоятельных новостных эпизодов, каждый из которых можно отдельно опубликовать как самостоятельную новость.
- unknown: недостаточно уверенности, особенно если есть несколько похожих Stories. Никогда не выбирай Story только потому, что совпали персона, страна или тема.

ОБЯЗАТЕЛЬНО:
1. Ищи конкретные anchors: действие, решение, инцидент, объект/место и конкретный повод.
2. Сначала определи, есть ли у всего RAW один общий основной сюжет.
3. Если несколько событий, заявлений, действий или последствий относятся к одной развивающейся ситуации и помогают описать её развитие, это ОДИН сюжет, а не digest.
4. Контекст, предыстория, заявление другого участника, связанное решение, отмена/изменение рейса, последствия или следующий этап той же ситуации сами по себе НЕ делают материал дайджестом.
5. Ставь digest только тогда, когда в RAW действительно есть несколько независимых сюжетов с разными поводами, которые можно разделить на отдельные публикации без потери смысла.
6. Если RAW содержит один общий сюжет и подходящей STORY нет, это new.
7. Если RAW содержит несколько независимых сюжетов, это digest.
8. Для update new_facts извлекай только из текущего RAW и только то, чего нет в конкретной STORY; максимум 1-3 коротких значимых факта.
9. Для new new_facts обязаны содержать 1-3 коротких значимых факта из текущего RAW. Если не можешь выделить такой факт, это unknown.
10. Не превращай заявление в установленный факт: сохраняй атрибуцию.
11. Не используй внешние знания и не додумывай.
12. update_text - только новый факт, максимум 2 коротких предложения.
13. reason - одна короткая фраза.
14. При конфликте RAW и FACTS доверяй RAW.
Верни только JSON с полями relation, story_id, confidence, new_facts, update_text, reason."""
        raw = await self._request_with_fallback(prompt, task="story_classify", max_tokens=600, temperature=0.0, system_prompt=story_system)
        if raw is None:
            raise RuntimeError("story classification unavailable")
        return self._parse(raw)

    @staticmethod
    def _select_update_facts(previous_title, previous_text, new_source, known_facts, limit=12):
        """Select the most relevant known Story facts for UPDATE classification."""
        facts = [str(f).strip() for f in (known_facts or []) if str(f).strip()]
        if len(facts) <= limit:
            return facts

        stopwords = {
            "это", "как", "так", "что", "кто", "где", "когда", "для",
            "при", "после", "перед", "между", "был", "была", "были",
            "есть", "будет", "будут", "также", "уже", "еще", "ещё",
            "или", "без", "под", "над", "через", "из", "от", "до",
            "по", "на", "во", "в", "с", "со", "и", "а", "но", "о",
            "об", "не", "ни", "за", "же", "ли", "из-за",
            "новый", "новая", "новое", "новые",
            "сообщил", "сообщила", "сообщили", "сообщает",
            "заявил", "заявила", "заявили", "заявляет",
            "рассказал", "рассказала", "рассказали",
        }

        def tokens(text):
            return {
                token
                for token in re.findall(r"[а-яёa-z0-9]{3,}", str(text).lower())
                if token not in stopwords
            }

        previous_tokens = tokens(" ".join([
            str(previous_title or ""),
            str(previous_text or ""),
        ]))
        new_tokens = tokens((new_source or {}).get("text", ""))

        if not previous_tokens and not new_tokens:
            return facts[:limit]

        ranked = []
        for index, fact in enumerate(facts):
            fact_tokens = tokens(fact)
            new_overlap = len(new_tokens & fact_tokens)
            previous_overlap = len(previous_tokens & fact_tokens)

            # The current material is the strongest relevance signal.
            # Previous publication context is only a secondary tie-breaker.
            score = new_overlap * 3 + previous_overlap
            ranked.append((score, new_overlap, previous_overlap, index, fact))

        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        return [fact for _, _, _, _, fact in ranked[:limit]]

    async def classify_update(self, previous_title, previous_text, new_source, known_facts=None):
        """Classify only materially significant updates; failures are explicit."""
        selected_facts = self._select_update_facts(
            previous_title,
            previous_text,
            new_source,
            known_facts,
            limit=12,
        )
        facts_text = "\n".join(f"- {fact}" for fact in selected_facts)
        if not facts_text:
            facts_text = "(нет накопленных фактов)"

        prompt = f"""
Ты очень строгий выпускающий редактор.

СТАРАЯ ПУБЛИКАЦИЯ:
ЗАГОЛОВОК: {previous_title}
ТЕКСТ: {previous_text}

РЕЛЕВАНТНЫЕ УЖЕ ИЗВЕСТНЫЕ ФАКТЫ STORY:
{facts_text}

НОВЫЙ МАТЕРИАЛ:
ИСТОЧНИК: {new_source.get('source', 'unknown')}
ТЕКСТ: {new_source.get('text', '')}

Нужно определить, есть ли ОСНОВАНИЕ для отдельной публикации UPDATE.

НЕ UPDATE:
- та же информация другими словами;
- повтор уже опубликованных фактов;
- повтор любого факта из представленной выборки уже известных фактов STORY, даже если он перефразирован;
- другой источник с тем же фактом;
- более длинный/подробный пересказ без нового самостоятельного эпизода;
- мелкая деталь, уточнение формулировки или очевидное следствие;
- мнение, оценка, прогноз или аналитика.

UPDATE только если появился НОВЫЙ СУЩЕСТВЕННЫЙ факт,
эпизод, действие, решение или изменение статуса по тому же событию,
которое уже описано в STORY.

Новый конкретный и проверяемый факт по тому же событию МОЖЕТ быть
достаточным основанием для UPDATE, даже если он не является новым
самостоятельным эпизодом.

ОСОБО ВАЖНО: если раньше был известен только план, намерение, ожидание
или предстоящее действие, а новый материал сообщает о фактически
произошедшем результате, принятом решении или изменении статуса,
это считается НОВЫМ СУЩЕСТВЕННЫМ UPDATE. Не считай такой результат
повтором ранее известного плана или намерения.

Например: ранее сообщалось, что следствие будет добиваться ареста,
а новый материал сообщает, что суд фактически избрал конкретную
меру пресечения и указал срок. Это существенное изменение статуса
и должно получить score >= 80.

Особенно учитывай как потенциально существенные:
- причину происшествия;
- число погибших, пострадавших или эвакуированных;
- конкретный результат или последствия происшествия;
- новое решение или изменение статуса;
- новое конкретное действие;
- новый объект или участника события;
- другую важную деталь, которая заметно меняет понимание уже известного события.

Для продолжающегося события новый самостоятельный эпизод также может быть UPDATE:
новая встреча, новый удар/эпизод, новое решение, новый объект, новый
результат или иное существенное развитие, которого нет в старой публикации.

НЕ считать UPDATE:
- ту же информацию другими словами;
- повтор уже опубликованного факта;
- повтор любого факта из представленной выборки уже известных фактов STORY;
- мелкую деталь, не меняющую понимание события;
- очевидное следствие;
- мнение, оценку, прогноз или аналитику.

Оценивай НОВОСТНУЮ СУЩЕСТВЕННОСТЬ нового факта, а не только формальную новизну.

ШКАЛА:
0-59  дубль, перефразировка, очевидное следствие или несущественная деталь;
60-79 новая информация по событию, но её ценность для отдельного короткого UPDATE сомнительна;
80-94 существенный новый факт по уже известному событию, который стоит сообщить;
95-100 критическое развитие или существенное изменение статуса.

КРИТИЧЕСКОЕ ПРАВИЛО:
score >= 80 означает, что UPDATE действительно следует публиковать.
score < 80 означает, что UPDATE публиковать нельзя.
Поле important всегда должно быть равно (score >= 80).
Не ставь score 80+ за формальную новизну без реальной новостной ценности.
Но не занижай score только потому, что новый факт является уточнением уже известного события.

Верни только JSON:
{{"important": false, "score": 0, "reason": "какой именно новый факт есть или почему его недостаточно"}}
"""
        raw = await self._request_with_fallback(prompt, task="update_check", max_tokens=500, temperature=0.0, system_prompt=UPDATE_CHECK_SYSTEM)
        if raw is None:
            return {"status": "unavailable", "important": False, "score": 0.0, "reason": "update_check_unavailable"}
        data = self._parse(raw)
        if not isinstance(data, dict):
            raise ValueError("Update classification result must be an object")
        try:
            score = max(0.0, min(100.0, float(data.get("score", 0))))
        except (TypeError, ValueError):
            score = 0.0
        return {"status": "ok", "important": score >= 80.0, "score": score,
                "reason": str(data.get("reason", "")).strip()}

    async def compose_event_update(self, previous_title, previous_text, new_source):
        prompt = f"""
Ты выпускающий редактор новостей.

СТАРАЯ ПУБЛИКАЦИЯ:
{previous_title}
{previous_text}

НОВЫЙ ИСТОЧНИК ({new_source.get('source', 'unknown')}):
{new_source.get('text', '')}

Сформируй UPDATE только из существенного нового факта, который отсутствует
в старой публикации. Не пересказывай старую публикацию.

ФОРМАТ UPDATE:
- title всегда должен быть пустым;
- text должен содержать только новый существенный факт;
- text должен быть ОДНОЙ строкой;
- максимум 1 короткое предложение;
- не повторяй старую публикацию;
- не добавляй вступление, заголовок, вывод или пояснение.

Если существенного нового факта нет, publish=false.
Не добавляй внешние знания, причины, последствия, прогнозы, оценки или аналитику.
Сохраняй атрибуцию заявлений.

Верни только JSON:
{{"publish": true, "title": "", "text": "", "confidence": 0.0, "reason": ""}}
"""
        raw = await self._request_with_fallback(prompt, task="update_compose", max_tokens=900, temperature=0.15, system_prompt=UPDATE_COMPOSE_SYSTEM)
        if raw is None:
            return {"status": "unavailable", "publish": False, "title": "", "text": "", "confidence": 0.0, "reason": "update_compose_unavailable"}
        data = self._parse(raw)
        if not isinstance(data, dict):
            raise ValueError("Update composition result must be an object")
        publish = data.get("publish", False)
        if isinstance(publish, str):
            publish = publish.strip().lower() in {"true", "1", "yes", "да"}
        title = ""
        body = " ".join(str(data.get("text", "")).split())
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        # Never publish obvious analytical output.
        if self._is_non_news_text(f"{title} {body}"):
            publish = False
        if not body:
            publish = False
        return {"status": "ok", "publish": bool(publish), "title": title, "text": body,
                "confidence": confidence, "reason": str(data.get("reason", "")).strip()}

    async def compose_threads(self, title, text, kind="NEW", max_length=480):
        title = " ".join(str(title or "").split()).strip()
        text = " ".join(str(text or "").split()).strip()
        kind = str(kind or "NEW").strip().upper()

        try:
            limit = max(100, min(int(max_length), 500))
        except (TypeError, ValueError):
            limit = 480

        prefix = "UPDATE: " if kind == "UPGRADE" else ""

        if title and text:
            source_text = f"{prefix}{title}\n\n{text}"
        else:
            source_text = f"{prefix}{title or text}"

        if not source_text:
            return {
                "status": "unavailable",
                "text": "",
                "reason": "empty_source_text",
            }

        prompt = f"""
Ты готовишь отдельную версию уже опубликованной новости для Threads.

ТИП ПУБЛИКАЦИИ:
{kind}

ИСХОДНАЯ ГОТОВАЯ ПУБЛИКАЦИЯ:
{source_text}

ЛИМИТ:
Не более {limit} знаков вместе со всеми символами.

Перепиши публикацию семантически для Threads.

Правила:
- Сохрани главный новостной факт.
- Сохрани существенные цифры, имена, места и атрибуцию.
- Не добавляй новых фактов.
- Не используй внешние знания.
- Не меняй смысл.
- Удали повторы, воду и второстепенные детали.
- Не обрезай текст механически.
- Результат должен быть самостоятельным и законченным.
- Не добавляй хештеги или служебный текст.
- Верни только JSON.

Формат:
{{"text": "..."}}
"""

        raw = await self._request_with_fallback(
            prompt,
            task="threads_compose",
            max_tokens=900,
            temperature=0.1,
            system_prompt=THREADS_SYSTEM,
        )

        if raw is None:
            return {
                "status": "unavailable",
                "text": "",
                "reason": "llm_unavailable",
            }

        try:
            data = self._parse(raw)
            result = " ".join(
                str(data.get("text", "")).split()
            ).strip()
        except Exception as exc:
            log.warning(
                "THREADS compose invalid response error=%s",
                exc,
            )
            return {
                "status": "unavailable",
                "text": "",
                "reason": "invalid_llm_response",
            }

        if not result:
            return {
                "status": "unavailable",
                "text": "",
                "reason": "empty_llm_response",
            }

        if len(result) > limit:
            log.warning(
                "THREADS compose exceeded limit length=%s limit=%s",
                len(result),
                limit,
            )
            return {
                "status": "unavailable",
                "text": "",
                "reason": "llm_result_exceeds_limit",
            }

        return {
            "status": "ok",
            "text": result,
            "reason": "",
        }

    async def edit(self, sources):
        if not sources:
            raise ValueError("LLM.edit() received no sources")
        source_text = " ".join(str(source.get("text", "")) for source in sources)
        if self._is_non_news_text(source_text):
            log.info("LLM editorial rejected digest_or_analytics")
            return {"publish": False, "title": "", "text": "", "category": "other",
                    "priority": 0, "confidence": 0.0, "reason": "digest_or_analytics", "is_digest": True}
        prompt = self._build_prompt(sources)
        raw = await self._request_with_fallback(prompt, task="editorial", max_tokens=900, temperature=0.25)
        if raw is None:
            raise RuntimeError("All LLM providers failed")
        raw_data = self._parse(raw)

        if raw_data.get("is_digest"):
            adjudication, adjudication_reason = await self._check_digest(sources)

            if adjudication is False:
                log.info(
                    "LLM digest adjudication overruled initial digest; "
                    "retrying editorial as single story"
                )

                retry_prompt = f"""
Ты редактор новостей. Предыдущий классификатор ошибочно решил, что материал является
дайджестом, но контролёр установил, что это ОДИН новостной сюжет.

Сформируй из материала одну полноценную новость.

ВАЖНО:
- выбери один главный информационный повод по заголовку и началу материала;
- остальные заявления, реакции, решения, отмены рейсов и действия других участников,
  если они объясняют, подтверждают, развивают или являются последствиями той же ситуации,
  используй только как контекст;
- НЕ превращай такой материал в дайджест;
- не добавляй фактов от себя;
- верни только JSON.

Материал:
{source_text}

Формат:
{{"publish": true, "title": "...", "text": "...",
"category": "other", "priority": 0, "confidence": 0.0,
"reason": "...", "is_digest": false, "hashtags": []}}
"""

                retry_raw = await self._request_with_fallback(
                    retry_prompt,
                    task="editorial_single_story",
                    max_tokens=900,
                    temperature=0.2,
                )

                if retry_raw is not None:
                    raw_data = self._parse(retry_raw)
                else:
                    log.warning(
                        "LLM single-story editorial retry unavailable; "
                        "keeping initial digest rejection"
                    )
            elif adjudication is True:
                log.info("LLM digest adjudication confirmed digest")
            else:
                log.warning(
                    "LLM digest adjudication unavailable; keeping initial digest"
                )

        data = self._normalize(raw_data)
        if self._is_non_news_text(f"{data.get('title','')} {data.get('text','')}"):
            data.update({"publish": False, "title": "", "text": "", "reason": "digest_or_analytics"})
        if data.get("is_digest"):
            data.update({"publish": False, "title": "", "text": "", "reason": data.get("reason") or "digest"})
        # Normalize hashtags and reserve space for them inside the final limit.
        raw_tags = data.get("hashtags", [])
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.replace(",", " ").split()
        forbidden = {"#новости", "#срочно", "#важно", "#главное", "#события"}
        tags = []
        seen = set()
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                tag = re.sub(r"[^\wА-Яа-яЁё0-9_]", "", str(tag).strip().lstrip("#"))
                if not tag:
                    continue
                tag = "#" + tag
                if tag.lower() in forbidden or tag.lower() in seen:
                    continue
                seen.add(tag.lower())
                tags.append(tag)
        if not tags:
            tags = ["#Россия" if data["category"] == "russia" else "#НовостиСобытий"]
        while len(tags) < 3:
            candidate = {"politics":"#Политика", "svo":"#СВО", "russia":"#Россия", "world":"#Мир",
                         "economy":"#Экономика", "security":"#Безопасность", "other":"#СобытияТекущие"}[data["category"]]
            if candidate.lower() not in {x.lower() for x in tags} and candidate.lower() not in forbidden:
                tags.append(candidate)
            else:
                tags.append("#Факты")
        data["hashtags"] = tags[:3]
        if data.get("publish"):
            max_length = int(getattr(self.s, "max_post_length", 800))
            suffix = "\n\n" + " ".join(data["hashtags"])
            available = max_length - len(suffix)
            body = data["text"].strip()

            if available < 100:
                data["publish"] = False
                data["reason"] = "max_post_length_too_small"

            elif len(body) > available:
                compact_prompt = f"""
Ты выпускающий редактор новостей.

ЗАГОЛОВОК:
{data.get("title", "")}

ТЕКСТ:
{body}

Сократи текст СЕМАНТИЧЕСКИ до {available} знаков или меньше.

Правила:
- Сохрани главный новостной факт.
- Сохрани существенные цифры, имена, места и атрибуцию.
- Удали воду, повторы и второстепенные детали.
- Не добавляй внешних знаний.
- Не повторяй заголовок в тексте.
- Один абзац.
- Обычно 2-3 предложения.
- Текст должен быть законченным и грамматически полноценным.
- НЕЛЬЗЯ просто обрезать текст по символам.

Верни только JSON:
{{"text": ""}}
"""

                compact_raw = await self._request_with_fallback(
                    compact_prompt,
                    task="editorial_compact",
                    max_tokens=900,
                    system_prompt=COMPACT_SYSTEM,
                    temperature=0.1,
                )

                compact_body = ""

                if compact_raw is not None:
                    try:
                        compact_data = self._parse(compact_raw)
                        compact_body = str(
                            compact_data.get("text", "")
                        ).strip()
                    except Exception:
                        compact_body = ""

                if compact_body and len(compact_body) <= available:
                    body = compact_body
                    log.info(
                        "LLM editorial semantic compact applied old_len=%d new_len=%d limit=%d",
                        len(data["text"].strip()),
                        len(body),
                        available,
                    )
                else:
                    data["publish"] = False
                    data["reason"] = "generated_text_too_long_after_semantic_compact"

            data["text"] = body + suffix if data.get("publish") else ""
        return data
