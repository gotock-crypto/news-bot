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
8. Не использовать пропагандистские лозунги, эмоциональную истерику или оскорбления.
9. Если информация подтверждена только одним источником, учитывать это при confidence.
10. Официальное заявление государственного ведомства или должностного лица является самостоятельным источником информации: отсутствие второго независимого подтверждения само по себе НЕ является основанием для publish=false. В таком случае явно отражай атрибуцию в тексте (например: «Минобороны РФ заявило...»), не превращая заявление в независимо установленный факт.
11. Если источники противоречат друг другу — не придумывать, кто прав.
12. Не публиковать рекламу, поздравления, мемы, бытовые посты и очевидный флуд.
13. Основные категории:
   politics, svo, russia, world, economy, security

Очень важно:
- Не добавляй сведения, которых нет в предоставленных источниках.
- Не выдавай предположение за установленный факт.
- Не называй информацию подтвержденной, если подтверждения нет.
- Если событие действительно новостное, подготовь самостоятельный рерайт.
- Текст должен быть на русском языке.

Официальное заявление ведомства можно публиковать как новость о самом заявлении. При этом обязательно сохраняй атрибуцию источника.

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

Важно: официальное заявление ведомства не считать слухом только из-за отсутствия второго источника.
"""


class LLM:
    def __init__(self, s):
        self.s = s

    def _call_groq(self, key, model, max_tokens, temperature, prompt):
        if Groq is None:
            raise RuntimeError("Пакет groq не установлен. Выполни: pip install -U groq")
        client = Groq(api_key=key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
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

    def _call_mistral(self, key, model, max_tokens, temperature, prompt):
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
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
            raise RuntimeError(f"Invalid Mistral response: {data}") from exc

    @staticmethod
    def _parse(raw):
        if not raw:
            raise ValueError("LLM returned empty response")
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM response does not contain JSON object: {raw[:500]}")
        candidate = raw[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON from LLM: {candidate[:1000]}") from exc

    def _normalize(self, data):
        if not isinstance(data, dict):
            raise ValueError("LLM JSON must be an object")
        publish = data.get("publish", False)
        if isinstance(publish, str):
            publish = publish.strip().lower() in {"true", "1", "yes", "да"}
        data["publish"] = bool(publish)
        data["title"] = str(data.get("title", "")).strip()
        data["text"] = str(data.get("text", "")).strip()
        data["category"] = str(data.get("category", "other")).strip().lower()
        if data["category"] not in {"politics", "svo", "russia", "world", "economy", "security", "other"}:
            data["category"] = "other"
        try:
            data["priority"] = max(0, min(10, int(data.get("priority", 0))))
        except (TypeError, ValueError):
            data["priority"] = 0
        try:
            data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            data["confidence"] = 0.0
        data["reason"] = str(data.get("reason", "")).strip()
        if not data["title"] or not data["text"]:
            data["publish"] = False
            data["reason"] = data["reason"] or "LLM не сформировал полный материал"
        max_length = getattr(self.s, "max_post_length", 3500)
        if len(data["text"]) > max_length:
            data["text"] = data["text"][:max_length - 3].rstrip() + "..."
        return data

    @staticmethod
    def _build_prompt(sources):
        blocks = []
        for source in sources:
            source_name = source.get("source", "unknown")
            reliability = source.get("reliability", 0.7)
            url = source.get("url", "")
            text = source.get("text", "")
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
        joined = "\n\n--- ИСТОЧНИК ---\n\n".join(blocks)
        return f"""
Ниже приведены сообщения из Telegram-источников.

Определи, описывают ли они одно реальное новостное событие.

Если да:
- выдели только подтверждаемые факты;
- подготовь самостоятельный русский новостной текст;
- не копируй исходные формулировки;
- укажи степень уверенности;
- выбери категорию;
- назначь приоритет;
- если единственный источник — официальное заявление ведомства/должностного лица, публикация допустима при наличии нормального новостного события; обязательно сохрани атрибуцию («заявило Минобороны РФ», «сообщили в ведомстве» и т.п.).

Если нет:
- publish=false;
- объясни причину в reason.

Материалы источников:

{joined}
"""

    async def edit(self, sources):
        if not sources:
            raise ValueError("LLM.edit() received no sources")
        prompt = self._build_prompt(sources)
        global LAST_REQUEST_AT
        async with LOCK:
            now = time.monotonic()
            wait = LLM_MIN_INTERVAL - (now - LAST_REQUEST_AT)
            if wait > 0:
                log.info("LLM rate limit: sleeping %.1fs before next request", wait)
                await asyncio.sleep(wait)

            providers = []
            if getattr(self.s, "groq_key", None):
                providers.append(("groq", self.s.groq_key, getattr(self.s, "groq_model", "qwen/qwen3.8-27b"), getattr(self.s, "groq_max_tokens", 1800), getattr(self.s, "groq_temperature", 0.35)))
            if getattr(self.s, "mistral_key", None):
                providers.append(("mistral", self.s.mistral_key, getattr(self.s, "mistral_model", "mistral-small-latest"), getattr(self.s, "mistral_max_tokens", 1800), getattr(self.s, "mistral_temperature", 0.35)))
            if not providers:
                raise RuntimeError("No LLM API key configured: set GROQ_API_KEY or MISTRAL_API_KEY")

            last_error = None
            for name, key, model, max_tokens, temperature in providers:
                try:
                    log.info("LLM request provider=%s model=%s", name, model)
                    LAST_REQUEST_AT = time.monotonic()
                    if name == "groq":
                        raw = await asyncio.to_thread(self._call_groq, key, model, max_tokens, temperature, prompt)
                    else:
                        raw = await asyncio.to_thread(self._call_mistral, key, model, max_tokens, temperature, prompt)
                    data = self._normalize(self._parse(raw))
                    log.info(
                        "LLM provider=%s category=%s priority=%s confidence=%.2f publish=%s",
                        name, data["category"], data["priority"], data["confidence"], data["publish"],
                    )
                    return data
                except Exception as exc:
                    last_error = exc
                    log.warning("LLM %s failed: %s", name, exc)
            raise RuntimeError(f"All LLM providers failed: {last_error}")
