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

Сравни НОВЫЙ материал с кандидатами из последних часов.
Определи отношение:
- new — другое событие;
- duplicate — то же событие, но новых существенных фактов нет;
- update — то же событие, и в новом материале появился хотя бы один
  существенный новый факт: новые цифры, пострадавшие, последствия,
  подтверждение/опровержение, новый официальный комментарий или иной
  факт, которого нет в предыдущей публикации.

КРИТИЧЕСКОЕ ПРАВИЛО:
Если новый материал не добавляет существенного факта к выбранному событию,
это duplicate. Другая формулировка, другой источник или более длинный текст
сами по себе НЕ являются update.

Не считай новости одинаковыми только из-за общей темы, региона или имени.
Не придумывай факты.
Отвечай ТОЛЬКО JSON:

{
  "relation": "new|duplicate|update",
  "event_id": 0,
  "confidence": 0.0,
  "new_facts": false,
  "reason": "краткое объяснение"
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

    def _candidate_rows(self, hours=6, limit=80):
        """
        Read candidate events directly from the current v2 DB.
        Rejected events are deliberately excluded so they can never absorb
        a later legitimate story.
        """
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT
                    e.id AS event_id,
                    e.category,
                    e.status,
                    e.created_at,
                    a.title,
                    a.text,
                    m.source
                FROM events e
                JOIN articles a
                  ON a.event_id = e.id
                 AND a.kind IN ('NEW','UPGRADE')
                LEFT JOIN messages m
                  ON m.id = a.message_id
                WHERE e.created_at >= datetime('now', ?)
                  AND e.status != 'rejected'
                ORDER BY e.updated_at DESC, a.id DESC
                LIMIT ?
                """,
                (f"-{float(hours)} hours", int(limit)),
            ).fetchall()

        # Prefer the latest article per event.
        result = []
        seen = set()
        for row in rows:
            eid = int(row["event_id"])
            if eid in seen:
                continue
            seen.add(eid)
            result.append(row)
        return result

    @staticmethod
    def _text(row):
        return f"{row['title'] or ''}\n{row['text'] or ''}"

    def _rank(self, title, text, rows):
        incoming = f"{title}\n{text}"
        ranked = []
        for row in rows:
            score = similarity(incoming, self._text(row))
            ranked.append((score, row))
        ranked.sort(key=lambda x: x[0], reverse=True)

        # Low floor intentionally allows semantically different wording to
        # reach the LLM resolver. Exact/strong lexical matches are handled
        # before this layer by Pipeline.
        floor = float(
            getattr(self.s, "event_resolver_similarity_floor", 0.25)
        )
        max_candidates = int(
            getattr(self.s, "event_resolver_max_candidates", 12)
        )
        return [
            row for score, row in ranked
            if score >= floor
        ][:max_candidates]

    @staticmethod
    def _build_prompt(title, text, category, candidates):
        blocks = []
        for row in candidates:
            blocks.append(
                "\n".join(
                    [
                        f"EVENT_ID: {row['event_id']}",
                        f"КАТЕГОРИЯ: {row['category'] or ''}",
                        f"ИСТОЧНИК: {row['source'] or ''}",
                        f"ЗАГОЛОВОК: {row['title'] or ''}",
                        f"ТЕКСТ: {row['text'] or ''}",
                    ]
                )
            )

        return f"""
НОВЫЙ МАТЕРИАЛ
КАТЕГОРИЯ: {category}
ЗАГОЛОВОК: {title}
ТЕКСТ: {text}

КАНДИДАТЫ
{chr(10).join(blocks)}

Выбери один кандидат только если это действительно то же событие.

Алгоритм:
1. Сначала сравни конкретные факты.
2. Если факты уже присутствуют в кандидате -> duplicate.
3. Если событие то же, но есть новый существенный факт -> update.
4. Если это другое событие -> new, event_id=0.
"""

    async def _call(self, prompt):
        global LAST_REQUEST_AT

        async with LOCK:
            now = time.monotonic()
            interval = float(
                getattr(self.s, "event_resolver_min_interval_seconds", 1.0)
            )
            wait = interval - (now - LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)

            providers = []

            if getattr(self.s, "groq_key", None):
                providers.append(
                    (
                        "groq",
                        self.s.groq_key,
                        getattr(
                            self.s, "groq_model", "qwen/qwen3.8-27b"
                        ),
                    )
                )

            if getattr(self.s, "mistral_key", None):
                providers.append(
                    (
                        "mistral",
                        self.s.mistral_key,
                        getattr(
                            self.s, "mistral_model",
                            "mistral-small-latest",
                        ),
                    )
                )

            if not providers:
                raise RuntimeError("no LLM provider configured")

            last_error = None

            for name, key, model in providers:
                try:
                    LAST_REQUEST_AT = time.monotonic()

                    url = (
                        "https://api.groq.com/openai/v1/chat/completions"
                        if name == "groq"
                        else "https://api.mistral.ai/v1/chat/completions"
                    )

                    response = requests.post(
                        url,
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
                        timeout=getattr(self.s, "llm_timeout", 60),
                    )
                    response.raise_for_status()

                    raw = response.json()["choices"][0]["message"]["content"]
                    return _parse(raw)

                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "resolver provider failed provider=%s error=%s",
                        name,
                        exc,
                    )

            raise RuntimeError(f"all resolver providers failed: {last_error}")

    async def resolve(self, title, text, category):
        rows = self._candidate_rows(
            float(getattr(self.s, "dedup_hours", 6)),
            int(getattr(self.s, "event_resolver_max_candidates", 12)) * 5,
        )

        candidates = self._rank(title, text, rows)

        if not candidates:
            return {
                "relation": "new",
                "event_id": 0,
                "confidence": 1.0,
                "new_facts": None,
                "reason": "no recent event candidates",
            }

        try:
            data = await self._call(
                self._build_prompt(title, text, category, candidates)
            )
        except Exception as exc:
            # Fail-open: resolver outage must not stop ingestion.
            log.exception("resolver failed; treating message as NEW")
            return {
                "relation": "new",
                "event_id": 0,
                "confidence": 0.0,
                "new_facts": None,
                "reason": f"resolver unavailable: {exc}",
            }

        relation = str(data.get("relation", "new")).lower().strip()
        if relation not in {"new", "duplicate", "update"}:
            relation = "new"

        try:
            event_id = int(data.get("event_id", 0))
        except (TypeError, ValueError):
            event_id = 0

        try:
            confidence = max(
                0.0, min(1.0, float(data.get("confidence", 0)))
            )
        except (TypeError, ValueError):
            confidence = 0.0

        nf = data.get("new_facts")
        if isinstance(nf, str):
            nf = nf.strip().lower() in {"true", "1", "yes", "да"}
        elif nf is not None:
            nf = bool(nf)

        # Never trust UPDATE when the model itself says there are no new facts.
        if relation == "update" and nf is False:
            relation = "duplicate"

        valid_ids = {int(row["event_id"]) for row in candidates}
        if event_id not in valid_ids:
            relation, event_id = "new", 0

        threshold = float(
            getattr(self.s, "event_resolver_confidence", 0.65)
        )
        if relation != "new" and confidence < threshold:
            log.info(
                "resolver low confidence relation=%s confidence=%.2f "
                "threshold=%.2f -> NEW",
                relation,
                confidence,
                threshold,
            )
            relation, event_id = "new", 0

        return {
            "relation": relation,
            "event_id": event_id,
            "confidence": confidence,
            "new_facts": nf,
            "reason": str(data.get("reason", "")).strip(),
        }
