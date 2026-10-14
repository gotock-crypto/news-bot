import logging
import re
from .dedup import similarity

log = logging.getLogger("story_manager")


class StoryManager:
    """24h active-story layer. Retrieval may be lexical; the decision is semantic via LLM."""
    def __init__(self, settings, db, llm):
        self.s = settings
        self.db = db
        self.llm = llm

    @staticmethod
    def _identity_phrases(text):
        # Retrieval-only signal for stable named entities.
        # Final identity decision remains semantic via LLM.
        words = re.findall(
            r"[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9-]{2,}",
            text or "",
        )
        phrases = set()
        for i in range(len(words) - 1):
            phrase = " ".join(words[i:i + 2]).lower().replace("ё", "е")
            phrases.add(phrase)
        return phrases

    def _story_corpus(self, event_id, row):
        parts = [str(row["title"] or "")]

        facts = self.db.story_facts(event_id, limit=5)
        parts.extend(str(f["fact_text"] or "") for f in facts)

        messages = self.db.story_messages(event_id, limit=2)
        parts.extend(str(m["text"] or "")[:450] for m in messages)

        return "\n".join(x for x in parts if x)

    def _identity_candidate_rows(self, incoming, rows, ranked):
        phrases = self._identity_phrases(incoming)
        if not phrases:
            return []

        ranked_by_id = {
            int(row["event_id"]): score
            for score, row in ranked
        }

        matches = []

        for row in rows:
            eid = int(row["event_id"])
            corpus = self._story_corpus(eid, row).lower().replace("ё", "е")

            matched = [
                phrase for phrase in phrases
                if phrase in corpus
            ]

            if matched:
                matches.append(
                    (
                        len(matched),
                        ranked_by_id.get(eid, 0.0),
                        eid,
                        row,
                    )
                )

        matches.sort(
            key=lambda x: (x[0], x[1], x[2]),
            reverse=True,
        )

        maximum = int(
            getattr(self.s, "story_identity_candidates", 5)
        )

        return [row for _, _, _, row in matches[:maximum]]

    def _candidate_rows(self, title, text):
        rows = self.db.story_candidates(
            getattr(self.s, "story_hours", 24),
            getattr(self.s, "story_candidate_limit", 40),
        )
        incoming = f"{title}\n{text}"

        ranked = []
        for r in rows:
            eid = int(r["event_id"])
            corpus = self._story_corpus(eid, r)
            score = similarity(incoming, corpus)
            ranked.append((score, r))

        ranked.sort(
            key=lambda x: (x[0], int(x[1]["event_id"])),
            reverse=True,
        )

        maximum = int(getattr(self.s, "story_semantic_candidates", 10))
        selected = [r for _, r in ranked[:maximum]]

        identity_rows = self._identity_candidate_rows(
            incoming,
            rows,
            ranked,
        )

        selected_ids = {int(r["event_id"]) for r in selected}

        for row in identity_rows:
            if int(row["event_id"]) not in selected_ids:
                selected.append(row)
                selected_ids.add(int(row["event_id"]))

        log.info(
            "story candidates active=%d semantic=%d identity=%d",
            len(rows),
            min(maximum, len(ranked)),
            len(identity_rows),
        )

        return selected

    def _guard_candidate_rows(self, title, text):
        rows = self.db.story_candidates(
            getattr(self.s, "story_hours", 24),
            getattr(self.s, "story_candidate_limit", 40),
        )
        incoming = f"{title}\n{text}"

        ranked = []

        for row in rows:
            eid = int(row["event_id"])
            corpus = self._story_corpus(eid, row)
            lexical_score = similarity(incoming, corpus)

            corpus_normalized = corpus.lower().replace("ё", "е")
            identity_hits = sum(
                1
                for phrase in self._identity_phrases(incoming)
                if phrase in corpus_normalized
            )

            ranked.append(
                (
                    identity_hits,
                    lexical_score,
                    eid,
                    row,
                )
            )

        ranked.sort(
            key=lambda x: (x[0], x[1], x[2]),
            reverse=True,
        )

        guard_limit = int(
            getattr(self.s, "story_new_guard_candidates", 3)
        )

        selected = [
            item
            for item in ranked[:guard_limit]
            if item[1] >= 0.05 or item[0] > 0
        ]
        selected_ids = {item[2] for item in selected}

        ranked_by_id = {
            item[2]: item
            for item in ranked
        }

        identity_rows = self._identity_candidate_rows(
            incoming,
            rows,
            [
                (item[1], item[3])
                for item in ranked
            ],
        )

        for row in identity_rows:
            eid = int(row["event_id"])
            item = ranked_by_id[eid]

            if eid not in selected_ids:
                selected.append(item)
                selected_ids.add(eid)

        return selected

    def _context(self, rows):
        blocks = []
        for row in rows:
            facts = self.db.story_facts(row["event_id"], limit=5)
            messages = self.db.story_messages(row["event_id"], limit=2)
            fact_text = "\n".join(f"- {f['fact_text']}" for f in facts) or "(facts not yet indexed)"
            raw_text = "\n".join(
                f"- [{m['source']}] RAW: {m['text'][:450]}" for m in messages
            ) or "(no raw messages)"
            blocks.append(
                f"STORY_ID: {row['event_id']}\n"
                f"TITLE: {row['title'] or ''}\n"
                f"FACTS (secondary, may be wrong):\n{fact_text}\n"
                f"RAW_MESSAGES (canonical source):\n{raw_text}"
            )
        return "\n\n--- STORY ---\n\n".join(blocks) if blocks else "(no active stories)"

    async def classify(self, title, text, category, raw_title=None, raw_text=None):
        rows = self._candidate_rows(title, text)
        raw_title = title if raw_title is None else raw_title
        raw_text = text if raw_text is None else raw_text
        context = f"""
НОВОЕ RAW-СООБЩЕНИЕ — ГЛАВНЫЙ ИСТОЧНИК ИСТИНЫ
SOURCE_TITLE: {raw_title}
SOURCE_TEXT: {raw_text}

РЕДАКТОРСКИЙ ЧЕРНОВОЙ РЕЗУЛЬТАТ — ВТОРИЧЕН
CATEGORY: {category}
TITLE: {title}
TEXT: {text}

АКТИВНЫЕ STORIES (последние 24 часа)
{self._context(rows)}

ПРАВИЛА:
1. Story = один конкретный реальный эпизод/развитие. Не тема, персона, страна, война, конфликт или набор связанных новостей.
2. Совпадение только по общему человеку, стране, организации, объекту, теме или времени НЕ является основанием для existing Story.
3. Сначала ищи конкретные anchors: одно действие/решение/заявление/инцидент, один объект или место, один конкретный повод.
4. Если anchors относятся к разным эпизодам — relation=new, даже если они тематически связаны.
5. duplicate — новый RAW сообщает тот же конкретный эпизод и не добавляет существенного нового факта. Перефразировка и новый источник с тем же фактом = duplicate.
6. update — тот же конкретный эпизод, но RAW добавляет существенное новое действие, результат, решение, статус или иной факт, которого нет в истории.
7. Не называй update то, что является только мнением, оценкой, прогнозом, более подробным пересказом или мелкой деталью.
8. new — нет подходящей Story. Для new обязательно дай 1–5 new_facts, прямо подтверждённых текущим RAW.
9. Для update new_facts должны быть только новыми фактами текущего RAW; update_text — только об этих фактах, максимум 1–2 коротких предложения.
10. Если не можешь надёжно отличить две Stories — unknown. UNKNOWN никогда не превращается в NEW автоматически.
11. Если FACTS противоречат RAW_MESSAGES, доверяй RAW_MESSAGES. Не исправляй RAW своими знаниями.
12. Не используй внешние знания.
"""
        result = self.normalize_result(await self.llm.classify_story(context))
        log.info("STORY MODEL decision relation=%s story=%s confidence=%.2f reason=%s", result["relation"], result["story_id"], result["confidence"], result["reason"][:300])

        if result["relation"] == "new":
            guard_candidates = self._guard_candidate_rows(
                raw_title,
                raw_text,
            )

            for identity_hits, lexical_score, candidate_id, candidate in guard_candidates:
                log.info(
                    "STORY NEW guard candidate=%s similarity=%.4f identity_hits=%d",
                    candidate_id,
                    lexical_score,
                    identity_hits,
                )

                confirm_context = f"""
СТРОГАЯ ПРОВЕРКА ПРЕДВАРИТЕЛЬНОГО NEW

RAW:
SOURCE_TITLE: {raw_title}
SOURCE_TEXT: {raw_text}

EDITORIAL:
TITLE: {title}
TEXT: {text}

КАНДИДАТ STORY:
{self._context([candidate])}

Предварительная модель выбрала NEW, но кандидат имеет lexical similarity {lexical_score:.4f}
и identity_hits={identity_hits}.

Проверь ТОЛЬКО эту пару.

duplicate = тот же конкретный эпизод без существенного нового факта.
update = тот же конкретный эпизод, но есть существенный новый факт, действие, результат или статус.
new = действительно другой конкретный эпизод.
unknown = недостаточно уверенности.

ВАЖНО:
Если кандидат и входящий материал описывают одно и то же конкретное событие, а новый материал
просто добавляет детали, новые цифры, даты, последствия, комментарии, источник, фото/видео
или уточнение обстоятельств  это НЕ NEW, а UPDATE.
Разница в формулировках заголовка, стиле подачи или названии действия сама по себе не делает Story новой.
Если новое сообщение подтверждает или переформулирует уже известное событие без существенного нового факта  это DUPLICATE.

Общий человек, страна, организация, объект, тема или время НЕ достаточны для existing Story.
Сравнивай конкретные anchors: действие, инцидент, место, объект, повод и результат.
RAW является главным источником истины.
Не используй внешние знания.

Верни тот же JSON-контракт Story Model.
Для duplicate/update обязательно укажи story_id={candidate_id}.
"""

                confirmed = self.normalize_result(
                    await self.llm.classify_story(confirm_context)
                )

                log.info(
                    "STORY NEW guard decision candidate=%s "
                    "similarity=%.4f identity_hits=%d relation=%s "
                    "story=%s confidence=%.2f reason=%s",
                    candidate_id,
                    lexical_score,
                    identity_hits,
                    confirmed["relation"],
                    confirmed["story_id"],
                    confirmed["confidence"],
                    confirmed["reason"][:300],
                )

                if confirmed["relation"] in {"duplicate", "update"}:
                    return confirmed

                if confirmed["relation"] == "unknown":
                    result = confirmed

        return result

    @staticmethod
    def normalize_result(raw):
        if not isinstance(raw, dict):
            raise ValueError("Story classification result must be an object")
        relation = str(raw.get("relation", "unknown")).strip().lower()
        if relation == "existing":
            relation = "duplicate"
        if relation not in {"new", "duplicate", "update", "digest", "unknown"}:
            relation = "unknown"
        try:
            story_id = int(raw.get("story_id", 0) or 0)
        except (TypeError, ValueError):
            story_id = 0
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        facts = raw.get("new_facts", [])
        if isinstance(facts, str):
            facts = [facts]
        facts = [str(x).strip() for x in facts if str(x).strip()]
        return {
            "relation": relation, "story_id": story_id, "confidence": confidence,
            "new_facts": facts[:10], "update_title": str(raw.get("update_title", "")).strip(),
            "update_text": str(raw.get("update_text", "")).strip(), "reason": str(raw.get("reason", "")).strip(),
        }
