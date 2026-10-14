import asyncio
import json
import logging
import re
import time

from .dedup import similarity
from ..llm.router import request as llm_request, groq_available

log = logging.getLogger("event_resolver")
LOCK = asyncio.Lock()
LAST_REQUEST_AT = 0.0

SYSTEM = """
Ты строгий редактор-идентификатор новостных событий.

Определи, относится ли новый материал к одному из уже опубликованных
событий. Сравнивай конкретный эпизод: что произошло, с кем, где, когда,
объект/цель, действие, статус и конкретные факты.

Правила:
- Одинаковая тема, персона, страна или общий контекст НЕ означают одно событие.
- Перефразировка, другой источник и более подробный пересказ НЕ делают событие новым.
- Если это тот же реальный эпизод, верни EXISTING и правильный event_id.
- Если это другое самостоятельное событие, верни NEW и event_id=0.
- Если по кандидатам нельзя надежно определить связь, верни UNKNOWN.
- Никогда не превращай неопределенность в NEW.
- Не используй внешние знания и не придумывай факты.

Верни только JSON:
{
  "relation": "new|existing|unknown",
  "event_id": 0,
  "confidence": 0.0,
  "same_episode": false,
  "reason": "краткое объяснение"
}
"""


def _parse(raw):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start = raw.find("{")
        if start < 0:
            raise ValueError("resolver response is not JSON")
        try:
            value, _ = decoder.raw_decode(raw[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("resolver response is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("resolver JSON must be an object")
    return value


class EventResolver:
    def __init__(self, settings, db):
        self.s = settings
        self.db = db

    def _candidate_rows(self, hours=None, limit=None):
        hours = float(hours if hours is not None else getattr(self.s, "event_resolver_hours", 24))
        limit = int(limit if limit is not None else getattr(self.s, "event_resolver_scan_limit", 200))
        with self.db.conn() as c:
            return c.execute(
                """
                WITH latest_articles AS (
                    SELECT a.*, ROW_NUMBER() OVER (
                        PARTITION BY a.event_id ORDER BY a.id DESC
                    ) AS rn
                    FROM articles a
                    JOIN events e ON e.id=a.event_id
                    JOIN publications p ON p.article_id=a.id
                    WHERE a.kind IN ('NEW','UPGRADE')
                      AND e.status='published'
                      AND e.updated_at >= datetime('now', ?)
                )
                SELECT e.id event_id, e.category, e.created_at, e.updated_at,
                       la.id article_id, la.message_id, la.kind, la.title,
                       la.text, la.confidence, m.source
                FROM events e
                JOIN latest_articles la ON la.event_id=e.id AND la.rn=1
                LEFT JOIN messages m ON m.id=la.message_id
                ORDER BY e.updated_at DESC, e.id DESC
                LIMIT ?
                """,
                (f"-{hours} hours", limit),
            ).fetchall()

    @staticmethod
    def _text(row):
        return f"{row['title'] or ''}\n{row['text'] or ''}"

    def _rank(self, title, text, rows):
        incoming = f"{title}\n{text}"
        ranked = [(similarity(incoming, self._text(row)), row) for row in rows]
        ranked.sort(key=lambda x: (x[0], int(x[1]["event_id"])), reverse=True)
        floor = float(getattr(self.s, "event_resolver_similarity_floor", .10))
        maximum = max(1, int(getattr(self.s, "event_resolver_max_candidates", 40)))
        selected = [(score, row) for score, row in ranked if score >= floor][:maximum]
        log.info(
            "resolver candidates=%d ranked=%d floor=%.2f max=%d top=%.3f",
            len(selected), len(ranked), floor, maximum, selected[0][0] if selected else 0.0,
        )
        return selected

    @staticmethod
    def _build_prompt(title, text, category, candidates):
        blocks = []
        for score, row in candidates:
            blocks.append(
                "\n".join([
                    f"EVENT_ID: {row['event_id']}",
                    f"LEXICAL_SCORE: {score:.3f}",
                    f"КАТЕГОРИЯ: {row['category'] or ''}",
                    f"ИСТОЧНИК: {row['source'] or ''}",
                    f"ЗАГОЛОВОК: {row['title'] or ''}",
                    f"ТЕКСТ: {(row['text'] or '')[:1400]}",
                ])
            )
        return f"""
НОВЫЙ МАТЕРИАЛ
КАТЕГОРИЯ: {category}
ЗАГОЛОВОК: {title}
ТЕКСТ: {text}

КАНДИДАТЫ
{chr(10).join(blocks) if blocks else '(нет кандидатов)'}

РЕШЕНИЕ:
1. Найди конкретный эпизод нового материала.
2. Сопоставляй только с фактическим содержанием кандидатов.
3. Если кандидат описывает тот же эпизод, верни existing с его EVENT_ID.
4. Перефразировка, подтверждение того же факта и дополнительные слова — existing.
5. Новый самостоятельный эпизод/событие — new.
6. Если нельзя уверенно отличить existing от new — unknown.
7. Никогда не выбирай new только потому, что совпадение слабое.
8. same_episode=true разрешено только если новый материал и кандидат описывают один конкретный реальный эпизод, а не просто одну тему.
9. Для relation=existing обязательно укажи event_id кандидата.
10. Для relation=new event_id всегда должен быть 0.

Верни только JSON.
"""

    async def _call(self, prompt):
        global LAST_REQUEST_AT
        async with LOCK:
            now = time.monotonic()
            interval = float(getattr(self.s, "event_resolver_min_interval_seconds", 4))
            wait = interval - (now - LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)

            providers = []
            if getattr(self.s, "groq_key", ""):
                providers.append(("groq", self.s.groq_key, getattr(self.s, "groq_model", "qwen/qwen3-27b")))
            if getattr(self.s, "gigachat_key", ""):
                providers.append(("gigachat", self.s.gigachat_key, getattr(self.s, "gigachat_model", "GigaChat-2")))
            if getattr(self.s, "mistral_key", ""):
                providers.append(("mistral", self.s.mistral_key, getattr(self.s, "mistral_model", "mistral-small-latest")))
            if not providers:
                raise RuntimeError("no LLM provider configured")

            last_error = None
            for name, key, model in providers:
                if name == "groq" and not groq_available():
                    log.info("resolver provider=groq cooldown active; using next provider")
                    continue
                try:
                    LAST_REQUEST_AT = time.monotonic()
                    extra = {"response_format": {"type": "json_object"}} if name == "groq" else None
                    raw = await asyncio.to_thread(
                        llm_request,
                        provider=name, key=key, model=model,
                        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                        max_tokens=300, temperature=0.0,
                        timeout=getattr(self.s, "llm_timeout", 60),
                        min_interval=getattr(self.s, "llm_min_interval_seconds", 4),
                        groq_cooldown=getattr(self.s, "groq_cooldown_seconds", 30),
                        extra=extra,
                    )
                    return _parse(raw)
                except Exception as exc:
                    last_error = exc
                    log.warning("resolver provider failed provider=%s error=%s", name, exc)
            raise RuntimeError(f"resolver unavailable: {last_error}")

    async def resolve(self, title, text, category):
        rows = self._candidate_rows()
        ranked = self._rank(title, text, rows)

        # No lexical candidates means there is no published event in the
        # resolver search window close enough to this material to compare.
        # Treat this as a safe NEW and continue through the editorial/scoring
        # pipeline. Do not ask the LLM to turn an empty candidate set into
        # UNKNOWN, which would incorrectly block genuinely new news.
        if not ranked:
            result = {
                "relation": "new",
                "event_id": 0,
                "confidence": 1.0,
                "same_episode": False,
                "reason": "no_candidates",
            }
            log.info(
                "resolver result relation=new event=0 confidence=1.00 reason=no_candidates"
            )
            return result

        prompt = self._build_prompt(title, text, category, ranked)
        try:
            raw = await self._call(prompt)
        except Exception as exc:
            log.warning("resolver unavailable: %s", exc)
            return {"relation": "unknown", "event_id": 0, "confidence": 0.0, "reason": "resolver_unavailable"}

        relation = str(raw.get("relation", "unknown")).strip().lower()
        if relation == "duplicate":
            relation = "existing"
        if relation not in {"new", "existing", "unknown"}:
            relation = "unknown"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            event_id = int(raw.get("event_id", 0) or 0)
        except (TypeError, ValueError):
            event_id = 0
        same_episode = raw.get("same_episode", False)
        if isinstance(same_episode, str):
            same_episode = same_episode.strip().lower() in {
                "true", "1", "yes", "да"
            }
        else:
            same_episode = bool(same_episode)

        rejected_existing = False

        if relation == "existing" and event_id <= 0:
            relation, event_id = "unknown", 0
            rejected_existing = True

        # Never merge events unless the model explicitly confirms that
        # both materials describe the same concrete real-world episode.
        existing_threshold = max(
            0.90,
            float(getattr(self.s, "event_resolver_confidence", 0.78)),
        )

        if relation == "existing" and (
            not same_episode or confidence < existing_threshold
        ):
            log.info(
                "resolver existing rejected same_episode=%s confidence=%.2f threshold=%.2f",
                same_episode,
                confidence,
                existing_threshold,
            )
            relation, event_id = "unknown", 0
            rejected_existing = True
        if relation == "new":
            event_id = 0
        # Low-confidence existing/new decisions are not safe enough for automatic routing.
        threshold = float(getattr(self.s, "event_resolver_confidence", .78))
        if relation in {"new", "existing"} and confidence < threshold:
            relation = "unknown"
            event_id = 0
        result = {
            "relation": relation,
            "event_id": event_id,
            "confidence": confidence,
            "same_episode": same_episode,
            "rejected_existing": rejected_existing,
            "reason": str(raw.get("reason", "")).strip(),
        }
        log.info("resolver result relation=%s event=%s confidence=%.2f reason=%s",
                 result["relation"], result["event_id"], result["confidence"], result["reason"][:180])
        return result
