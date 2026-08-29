import asyncio
import json
import logging
import re
import time

import requests

from .dedup import similarity

log = logging.getLogger("event_resolver")
LOCK = asyncio.Lock()
LAST_REQUEST_AT = 0.0

SYSTEM = """
Ты — редактор-дедупликатор новостной ленты.

Сравни НОВЫЙ материал с кандидатами из последних нескольких часов.
Определи отношение:
- new — другое событие;
- duplicate — то же событие и новых существенных фактов нет;
- update — то же событие, но появились существенные новые факты, цифры,
  пострадавшие, последствия, подтверждение или опровержение.

Важно:
- Не считай новости одинаковыми только потому, что совпадают регион,
  общая тема или одно имя.
- Если это тот же инцидент, но число пострадавших или другие существенные
  обстоятельства уточнились, используй update.
- Не придумывай факты.
- Отвечай ТОЛЬКО JSON.

Формат:
{
  "relation": "new|duplicate|update",
  "event_id": 0,
  "confidence": 0.0,
  "reason": "кратко почему"
}
"""


def _parse(raw):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("resolver response is not JSON")
        return json.loads(raw[start:end + 1])


class EventResolver:
    def __init__(self, settings, db):
        self.s = settings
        self.db = db

    @staticmethod
    def _text(row):
        return f"{row['title']}\n{row['text']}"

    def _rank(self, title, text, rows):
        incoming = f"{title}\n{text}"
        ranked = []
        for row in rows:
            score = similarity(incoming, self._text(row))
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        floor = float(self.s.event_resolver_similarity_floor)
        return [row for score, row in ranked if score >= floor][: int(self.s.event_resolver_max_candidates)]

    @staticmethod
    def _build_prompt(title, text, category, candidates):
        blocks = []
        for row in candidates:
            blocks.append(
                "\n".join([
                    f"EVENT_ID: {row['event_id']}",
                    f"КАТЕГОРИЯ: {row['category']}",
                    f"ИСТОЧНИК: {row['source']}",
                    f"ЗАГОЛОВОК: {row['title']}",
                    f"ТЕКСТ: {row['text']}",
                ])
            )
        joined = "\n\n--- КАНДИДАТ ---\n\n".join(blocks)
        return f"""
НОВЫЙ МАТЕРИАЛ
КАТЕГОРИЯ: {category}
ЗАГОЛОВОК: {title}
ТЕКСТ: {text}

КАНДИДАТЫ ИЗ ОКНА ПОСЛЕДНИХ ЧАСОВ
{joined}

Выбери один лучший кандидат только если это действительно то же событие.
Если подходящего кандидата нет — relation=new и event_id=0.
"""

    async def _call(self, prompt):
        global LAST_REQUEST_AT
        async with LOCK:
            now = time.monotonic()
            wait = float(self.s.event_resolver_min_interval_seconds) - (now - LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)

            providers = []
            if getattr(self.s, "groq_key", None):
                providers.append((
                    "groq",
                    self.s.groq_key,
                    getattr(self.s, "groq_model", "qwen/qwen3-27b"),
                ))
            if getattr(self.s, "mistral_key", None):
                providers.append((
                    "mistral",
                    self.s.mistral_key,
                    getattr(self.s, "mistral_model", "mistral-small-latest"),
                ))

            if not providers:
                raise RuntimeError("no LLM provider configured")

            last_error = None
            for name, key, model in providers:
                try:
                    LAST_REQUEST_AT = time.monotonic()
                    if name == "groq":
                        response = requests.post(
                            "https://api.groq.com/openai/v1/chat/completions",
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
                                "max_tokens": 300,
                                "temperature": 0.1,
                            },
                            timeout=self.s.llm_timeout,
                        )
                    else:
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
                                "max_tokens": 300,
                                "temperature": 0.1,
                            },
                            timeout=self.s.llm_timeout,
                        )

                    response.raise_for_status()
                    payload = response.json()
                    raw = payload["choices"][0]["message"]["content"]
                    return _parse(raw)
                except Exception as exc:
                    last_error = exc
                    log.warning("resolver provider failed provider=%s error=%s", name, exc)

            raise RuntimeError(f"all resolver providers failed: {last_error}")

    async def resolve(self, title, text, category):
        rows = self.db.event_window_candidates(
            self.s.event_resolver_hours,
            max(1, int(self.s.event_resolver_max_candidates) * 4),
        )
        candidates = self._rank(title, text, rows)
        if not candidates:
            return {"relation": "new", "event_id": 0, "confidence": 1.0, "reason": "no recent semantic candidates"}

        prompt = self._build_prompt(title, text, category, candidates)
        try:
            data = await self._call(prompt)
        except Exception as exc:
            # Resolver не должен останавливать новостной pipeline.
            log.exception("resolver failed; fail-open as NEW")
            return {"relation": "new", "event_id": 0, "confidence": 0.0, "reason": f"resolver unavailable: {exc}"}

        relation = str(data.get("relation", "new")).strip().lower()
        if relation not in {"new", "duplicate", "update"}:
            relation = "new"

        try:
            event_id = int(data.get("event_id", 0))
        except (TypeError, ValueError):
            event_id = 0

        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0

        valid_ids = {int(row["event_id"]) for row in candidates}
        if event_id not in valid_ids:
            relation = "new"
            event_id = 0

        if relation != "new" and confidence < float(self.s.event_resolver_confidence):
            log.info(
                "resolver low confidence relation=%s confidence=%.2f threshold=%.2f -> NEW",
                relation,
                confidence,
                self.s.event_resolver_confidence,
            )
            relation = "new"
            event_id = 0

        return {
            "relation": relation,
            "event_id": event_id,
            "confidence": confidence,
            "reason": str(data.get("reason", "")).strip(),
        }
