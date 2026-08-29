import asyncio
import logging

from .config import load_settings
from .logging_setup import setup
from .db import DB
from .core.dedup import find_similar
from .core.event_resolver import EventResolver
from .llm.adapter import LLM
from .telegram.collector import TelegramCollector
from .max.publisher import MaxPublisher

log = logging.getLogger("app")


class App:
    def __init__(self):
        self.s = load_settings()
        setup(self.s.log_level)
        self.db = DB(self.s.db_path)
        cutoff = self.db.cleanup_retention(self.s.retention_days)
        log.info("DB retention cleanup complete retention_days=%s cutoff=%s", self.s.retention_days, cutoff)
        self.llm = LLM(self.s)
        self.event_resolver = EventResolver(self.s, self.db)
        self.max = MaxPublisher(self.s)

    async def handle_message(self, msg_id, source, backfill=False):
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()

        if not row:
            return

        recent = self.db.recent_messages(self.s.dedup_hours)
        others = [r for r in recent if r["id"] != msg_id]
        sim = find_similar(row["text"], others, self.s.similarity_threshold)

        if sim:
            existing = self.db.find_event_for_message(sim[1]["id"])
            if existing:
                eid = existing["event_id"]
                self.db.attach_event(eid, msg_id)
                try:
                    await self.process_event(eid, allow_publish=not backfill)
                except Exception:
                    log.exception("event update failed event=%s", eid)
                log.info(
                    "dedup -> event=%s source=%s similarity=%.3f backfill=%s",
                    eid,
                    row["source"],
                    sim[0],
                    backfill,
                )
                return

        try:
            data = await self.llm.edit([self._source_payload(row)])
            if not data.get("publish"):
                log.info(
                    "editor rejected msg=%s reason=%s backfill=%s",
                    msg_id,
                    data.get("reason"),
                    backfill,
                )
                return

            # Новый семантический слой работает только перед публикацией.
            # Старый similarity/event clustering выше остаётся без изменений.
            if not backfill:
                decision = await self.event_resolver.resolve(
                    data.get("title", ""),
                    data.get("text", ""),
                    data.get("category", "other"),
                )
                relation = decision["relation"]
                matched_eid = int(decision.get("event_id", 0))
                log.info(
                    "event resolver relation=%s event=%s confidence=%.2f reason=%s source=%s msg=%s",
                    relation,
                    matched_eid,
                    decision.get("confidence", 0.0),
                    decision.get("reason", ""),
                    source,
                    msg_id,
                )

                if relation in {"duplicate", "update"} and matched_eid:
                    self.db.attach_event(matched_eid, msg_id)
                    self.db.upsert_event_window(
                        matched_eid,
                        data.get("title", ""),
                        data.get("text", ""),
                        data.get("category", "other"),
                        source,
                        self.s.event_resolver_hours,
                    )

                    if relation == "duplicate":
                        log.info(
                            "publish SKIP event=%s reason=semantic_duplicate source=%s msg=%s",
                            matched_eid,
                            source,
                            msg_id,
                        )
                        return

                    # UPDATE проходит через существующий event/UPDATE pipeline.
                    await self.process_event(matched_eid, allow_publish=True)
                    return

            eid = self.db.create_event(
                data.get("title", ""),
                data.get("category", "other"),
                int(data.get("priority", 0)),
                float(data.get("confidence", 0)),
                msg_id,
            )
            self.db.set_article(
                eid,
                data.get("title", ""),
                data.get("text", ""),
                data.get("category", "other"),
                float(data.get("confidence", 0)),
                data.get("reason", ""),
            )

            # Кандидат попадает во временное 3h-окно до фактической публикации.
            self.db.upsert_event_window(
                eid,
                data.get("title", ""),
                data.get("text", ""),
                data.get("category", "other"),
                source,
                self.s.event_resolver_hours,
            )

            log.info(
                "article ready event=%s priority=%s confidence=%s publish=%s backfill=%s",
                eid,
                data.get("priority"),
                data.get("confidence"),
                data.get("publish"),
                backfill,
            )

            if backfill:
                log.info("publish SKIP event=%s reason=startup_backfill", eid)
            else:
                await self.maybe_publish(eid, data)

        except Exception:
            log.exception("processing failed msg=%s", msg_id)

    def _source_payload(self, row):
        return {
            "source": row["source"],
            "url": row["url"],
            "text": row["text"],
            "reliability": row["source_reliability"],
            "priority": row["source_priority"],
        }

    async def process_event(self, eid, allow_publish=True):
        rows = self.db.event_messages(eid)
        data = await self.llm.edit([self._source_payload(r) for r in rows])
        if not data.get("publish"):
            self.db.mark_rejected(eid, data.get("reason", ""))
            return

        self.db.update_event(
            eid,
            data.get("title", ""),
            data.get("category", "other"),
            int(data.get("priority", 0)),
            float(data.get("confidence", 0)),
        )
        self.db.set_article(
            eid,
            data.get("title", ""),
            data.get("text", ""),
            data.get("category", "other"),
            float(data.get("confidence", 0)),
            data.get("reason", ""),
        )

        if allow_publish:
            await self.maybe_publish(eid, data)
        else:
            log.info("publish SKIP event=%s reason=startup_backfill", eid)

    async def maybe_publish(self, eid, data):
        if not self.s.auto_publish:
            log.info("publish SKIP event=%s reason=AUTO_PUBLISH=0", eid)
            return

        priority = int(data.get("priority", 0))
        threshold = int(self.s.min_priority_publish)

        if priority < threshold:
            log.info(
                "publish SKIP event=%s reason=priority priority=%s threshold=%s",
                eid, priority, threshold,
            )
            return

        confidence = float(data.get("confidence", 0))
        if confidence < self.s.min_confidence_publish:
            log.info(
                "publish SKIP event=%s reason=confidence confidence=%.2f threshold=%.2f",
                eid, confidence, self.s.min_confidence_publish,
            )
            return

        if not data.get("publish"):
            log.info("publish SKIP event=%s reason=LLM_publish_false", eid)
            return

        log.info("MAX publish START event=%s priority=%s confidence=%.2f", eid, priority, confidence)
        try:
            await self.publish_event(eid)
        except Exception:
            log.exception("MAX publish FAILED event=%s", eid)
            raise

    async def publish_event(self, eid):
        with self.db.conn() as c:
            a = c.execute("SELECT * FROM articles WHERE event_id=?", (eid,)).fetchone()

        if not a:
            log.error("MAX publish FAILED event=%s reason=article_not_found", eid)
            return

        maxid = await self.max.publish(a["title"], a["text"], "")

        if maxid is None:
            log.error("MAX publish FAILED event=%s reason=no_message_id", eid)
            return

        self.db.mark_published(eid, maxid)
        log.info("MAX publish SUCCESS event=%s max_message_id=%s", eid, maxid)

    async def start_max_safe(self):
        try:
            await self.max.start()
        except Exception:
            log.exception("MAX startup failed; Telegram processing will continue")

    async def run(self):
        collector = TelegramCollector(self.s, self.db, self.handle_message)
        try:
            await asyncio.gather(
                self.start_max_safe(),
                collector.start(),
            )
        except asyncio.CancelledError:
            log.info("App shutdown requested")
            raise
