import asyncio
import json
import logging
import re
import time

import requests

from .dedup import similarity
from ..llm.router import (
    request as llm_request,
    groq_available,
)

log = logging.getLogger("event_resolver")

LOCK = asyncio.Lock()
LAST_REQUEST_AT = 0.0

SYSTEM = """
Ты — строгий редактор-идентификатор новостных событий.

Твоя задача №1 — определить, описывает ли НОВЫЙ материал то же РЕАЛЬНОЕ
СОБЫТИЕ, что один из кандидатов. Сравнивай конкретные факты: что произошло,
где, с кем, когда, объект/цель, числа, статус и последствия.

ВАЖНО:
- Одинаковая тема, страна, город, персона или общий фон НЕ означают одно событие.
- Другая формулировка, другой источник и более длинный текст сами по себе
  не делают событие новым.
- Если это то же событие, отношение должно быть duplicate или update.
- Если это то же событие, но новый материал содержит существенный новый факт,
  допускается update.
- Если существенных новых фактов нет — duplicate.
- Если событие другое — new и event_id=0.
- Не выбирай кандидата только потому, что он лексически похож.
- Не придумывай факты и не используй внешние знания.

ОСОБЕННО СТРОГО:
Соседние эпизоды одной войны, операции, протеста, аварии или расследования
могут иметь одинаковые слова и имена, но быть разными событиями. Смотри на
конкретный эпизод, а не на тему.

Верни только JSON:
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

    def _candidate_rows(self, hours=None, limit=None):
        """Return one latest article per non-rejected event in the time window.

        The old implementation applied LIMIT before collapsing rows to events.
        A busy event with several articles could therefore hide newer events from
        the resolver. This query collapses first and limits second.
        """
        if hours is None:
            hours = float(getattr(self.s, "event_resolver_hours", getattr(self.s, "dedup_hours", 24)))
        if limit is None:
            limit = int(getattr(self.s, "event_resolver_scan_limit", 200))

        with self.db.conn() as c:
            rows = c.execute(
                """
                WITH latest_articles AS (
                    SELECT
                        a.id,
                        a.event_id,
                        a.message_id,
                        a.kind,
                        a.title,
                        a.text,
                        a.confidence,
                        ROW_NUMBER() OVER (
                            PARTITION BY a.event_id
                            ORDER BY a.id DESC
                        ) AS rn
                    FROM articles a
                    JOIN events e ON e.id = a.event_id
                    WHERE a.kind IN ('NEW','UPGRADE')
                      AND e.updated_at >= datetime('now', ?)
                      AND e.status != 'rejected'
                )
                SELECT
                    e.id AS event_id,
                    e.category,
                    e.status,
                    e.created_at,
                    e.updated_at,
                    la.id AS article_id,
                    la.message_id,
                    la.kind,
                    la.title,
                    la.text,
                    la.confidence,
                    m.source
                FROM events e
                JOIN latest_articles la
                  ON la.event_id = e.id
                 AND la.rn = 1
                LEFT JOIN messages m ON m.id = la.message_id
                ORDER BY e.updated_at DESC, e.id DESC
                LIMIT ?
                """,
                (f"-{float(hours)} hours", int(limit)),
            ).fetchall()

        return rows

    @staticmethod
    def _text(row):
        return f"{row['title'] or ''}\n{row['text'] or ''}"

    def _rank(self, title, text, rows):
        incoming = f"{title}\n{text}"
        ranked = []
        for row in rows:
            score = similarity(incoming, self._text(row))
            ranked.append((score, row))
        ranked.sort(key=lambda x: (x[0], int(x[1]["event_id"])), reverse=True)

        floor = float(getattr(self.s, "event_resolver_similarity_floor", 0.10))
        max_candidates = max(
            1,
            int(getattr(self.s, "event_resolver_max_candidates", 40)),
        )

        selected = [
            (score, row)
            for score, row in ranked
            if score >= floor
        ][:max_candidates]

        log.info(
            "resolver candidates=%d ranked=%d floor=%.2f max=%d top=%.3f",
            len(selected),
            len(ranked),
            floor,
            max_candidates,
            selected[0][0] if selected else 0.0,
        )
        return selected

    @staticmethod
    def _build_prompt(title, text, category, candidates):
        blocks = []
        for score, row in candidates:
            blocks.append(
                "\n".join(
                    [
                        f"EVENT_ID: {row['event_id']}",
                        f"LEXICAL_SCORE: {score:.3f}",
                        f"КАТЕГОРИЯ: {row['category'] or ''}",
                        f"ИСТОЧНИК: {row['source'] or ''}",
                        f"ЗАГОЛОВОК: {row['title'] or ''}",
                        f"ТЕКСТ: {(row['text'] or '')[:1200]}",
                    ]
                )
            )

        return f"""
НОВЫЙ МАТЕРИАЛ
КАТЕГОРИЯ: {category}
ЗАГОЛОВОК: {title}
ТЕКСТ: {text}

КАНДИДАТЫ СОБЫТИЙ
{chr(10).join(blocks)}

Алгоритм:
1. Определи конкретный эпизод нового материала.
2. Сравни его с кандидатами по фактам, а не по общим словам.
3. Выбирай event_id только если это действительно тот же эпизод.
4. Если тот же эпизод и существенных новых фактов нет — duplicate.
5. Если тот же эпизод и есть существенный новый факт — update.
6. Если это другое событие — new, event_id=0.
7. При сомнении выбирай new.

Верни только JSON.
"""

    async def _call(self, prompt):
        global LAST_REQUEST_AT

        async with LOCK:
            now = time.monotonic()
            interval = float(
                getattr(self.s, "event_resolver_min_interval_seconds", 4.0)
            )
            wait = interval - (now - LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)

            providers = []
            if getattr(self.s, "groq_key", None):
                providers.append((
                    "groq",
                    self.s.groq_key,
                    getattr(self.s, "groq_model", "qwen/qwen3-27b"),
                ))

            if getattr(self.s, "gigachat_key", None):
                providers.append((
                    "gigachat",
                    self.s.gigachat_key,
                    getattr(self.s, "gigachat_model", "GigaChat-2"),
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
                # Groq уже на cooldown: не вызываем API вообще.
                # Сразу переходим к следующему провайдеру.
                if name == "groq" and not groq_available():
                    log.info(
                        "resolver provider=groq cooldown active; using next provider"
                    )
                    continue

                try:
                    extra = {}

                    if name == "groq":
                        extra = {
                            "reasoning_effort": "none",
                            "reasoning_format": "hidden",
                            "response_format": {
                                "type": "json_object"
                            },
                        }

                    raw = llm_request(
                        provider=name,
                        key=key,
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
                        max_tokens=300,
                        temperature=0.05,
                        timeout=getattr(
                            self.s,
                            "llm_timeout",
                            60,
                        ),
                        min_interval=getattr(
                            self.s,
                            "llm_min_interval_seconds",
                            4.0,
                        ),
                        groq_cooldown=getattr(
                            self.s,
                            "groq_cooldown_seconds",
                            30,
                        ),
                        extra=extra,
                    )

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
        rows = self._candidate_rows()
        ranked = self._rank(title, text, rows)

        if not ranked:
            return {
                "relation": "new",
                "event_id": 0,
                "confidence": 1.0,
                "new_facts": None,
                "reason": "no recent event candidates",
            }

        try:
            data = await self._call(
                self._build_prompt(title, text, category, ranked)
            )
        except Exception as exc:
            # Fail closed for strong lexical matches: attaching to an existing
            # event still lets classify_update decide duplicate vs update and
            # prevents an outage from creating obvious duplicate publications.
            top_score, top_row = ranked[0]
            fallback = float(
                getattr(self.s, "event_resolver_fallback_similarity", 0.94)
            )
            if top_score >= fallback:
                log.warning(
                    "resolver unavailable; strong-match fallback event=%s score=%.3f",
                    top_row["event_id"],
                    top_score,
                )
                return {
                    "relation": "duplicate",
                    "event_id": int(top_row["event_id"]),
                    "confidence": 0.0,
                    "new_facts": None,
                    "reason": f"resolver unavailable; lexical fallback score={top_score:.3f}",
                }

            log.exception("resolver failed; treating weak match as NEW")
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
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0

        nf = data.get("new_facts")
        if isinstance(nf, str):
            nf = nf.strip().lower() in {"true", "1", "yes", "да"}
        elif nf is not None:
            nf = bool(nf)

        valid_ids = {int(row["event_id"]) for _, row in ranked}
        if event_id not in valid_ids:
            relation, event_id = "new", 0

        threshold = float(getattr(self.s, "event_resolver_confidence", 0.78))
        if relation != "new" and confidence < threshold:
            top_score, top_row = ranked[0]
            fallback = float(
                getattr(self.s, "event_resolver_fallback_similarity", 0.94)
            )
            if top_score >= fallback:
                log.info(
                    "resolver low confidence; strong lexical fallback event=%s score=%.3f",
                    top_row["event_id"],
                    top_score,
                )
                relation, event_id = "duplicate", int(top_row["event_id"])
            else:
                log.info(
                    "resolver low confidence relation=%s confidence=%.2f threshold=%.2f -> NEW",
                    relation,
                    confidence,
                    threshold,
                )
                relation, event_id = "new", 0

        if relation == "update" and nf is False:
            relation = "duplicate"

        log.info(
            "resolver result relation=%s event=%s confidence=%.2f new_facts=%s reason=%s",
            relation,
            event_id,
            confidence,
            nf,
            str(data.get("reason", ""))[:180],
        )

        return {
            "relation": relation,
            "event_id": event_id,
            "confidence": confidence,
            "new_facts": nf,
            "reason": str(data.get("reason", "")).strip(),
        }
