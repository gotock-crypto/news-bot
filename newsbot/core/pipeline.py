import logging

from .event_resolver import EventResolver

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(
        self,
        s,
        db,
        llm,
        max_client,
        telegram_publisher=None,
        threads_publisher=None,
    ):
        self.s = s
        self.db = db
        self.llm = llm
        self.max = max_client
        self.telegram = telegram_publisher
        self.threads = threads_publisher
        self.resolver = EventResolver(s, db)

    def payload(self, r):
        return {
            "source": r["source"],
            "url": r["url"],
            "text": r["text"],
            "reliability": r["reliability"],
            "priority": r["priority"],
        }

    async def handle(self, msg_id, source):
        with self.db.conn() as c:
            r = c.execute(
                "SELECT * FROM messages WHERE id=?",
                (msg_id,),
            ).fetchone()

        if not r:
            return

        # ---------------------------------------------------------
        # 1. Exact duplicate: zero chance of publication.
        # ---------------------------------------------------------
        exact = self.db.exact_recent(
            r["norm_hash"],
            getattr(self.s, "dedup_hours", 6),
        )

        if exact and exact["id"] != r["id"]:
            rel = self.db.find_event_by_message(exact["id"])
            if rel:
                self.db.attach_event(rel["event_id"], msg_id)
                log.info(
                    "DUPLICATE exact event=%s msg=%s",
                    rel["event_id"],
                    msg_id,
                )
                return

        # ---------------------------------------------------------
        # 2. Semantic event resolver.
        #
        # Do NOT short-circuit through the old lexical find_match().
        # Semantic resolver must be the single decision point for:
        #   duplicate -> existing event, no publication
        #   update    -> existing event, possible upgrade
        #   new       -> create new event
        #
        # This is important for cross-source duplicates where wording,
        # names and formulations differ.
        # ---------------------------------------------------------

        # ---------------------------------------------------------
        # 3. Editorial LLM for a candidate NEW story.
        # ---------------------------------------------------------
        data = await self.llm.edit([self.payload(r)])

        # ---------------------------------------------------------
        # 4. Semantic event resolver.
        #
        # This is the missing v2 layer: messages from different sources
        # can describe the same event with substantially different wording.
        # ---------------------------------------------------------
        resolution = await self.resolver.resolve(
            data.get("title", ""),
            data.get("text", ""),
            data.get("category", "other"),
        )

        relation = resolution["relation"]
        event_id = int(resolution.get("event_id", 0) or 0)

        if relation in {"duplicate", "update"} and event_id:
            # Resolver decides EVENT ID only. The final duplicate-vs-upgrade
            # decision is always made against the latest published article.
            # This prevents the resolver from accidentally suppressing a real
            # update just because it classified the relation as duplicate.
            self.db.attach_event(event_id, msg_id)
            await self.existing_event(
                event_id,
                r,
                float(resolution.get("confidence", 0) or 0),
                resolver_relation=relation,
                resolver_new_facts=bool(
                    resolution.get("new_facts", False)
                ),
            )
            return

        # ---------------------------------------------------------
        # 5. Truly new event.
        # ---------------------------------------------------------
        eid = self.db.create_event(
            data.get("title", ""),
            data.get("category", "other"),
            int(data.get("priority", 0)),
            float(data.get("confidence", 0)),
            msg_id,
        )

        if not data.get("publish"):
            self.db.mark_rejected(
                eid,
                data.get("reason", ""),
            )
            return

        aid = self.db.add_article(
            eid,
            msg_id,
            "NEW",
            data.get("title", ""),
            data.get("text", ""),
            float(data.get("confidence", 0)),
            data.get("reason", ""),
        )

        await self.publish_new(eid, aid, data)

    async def existing_event(
        self,
        eid,
        r,
        score=0.0,
        resolver_relation="",
        resolver_new_facts=False,
    ):
        # Only published material is a valid parent for an upgrade.
        prev = self.db.last_publication(eid)

        if not prev:
            log.info(
                "EVENT existing but no publication event=%s; no upgrade",
                eid,
            )
            return

        check = await self.llm.classify_update(
            prev["title"] or "",
            prev["text"] or "",
            self.payload(r),
        )

        important = bool(check.get("important"))
        update_score = float(check.get("score", 0) or 0)

        log.info(
            "UPDATE decision event=%s important=%s score=%.1f "
            "reason=%s",
            eid,
            important,
            update_score,
            check.get("reason", ""),
        )

        resolver_override = (
            resolver_relation == "update"
            and score >= 0.95
            and resolver_new_facts
            and update_score >= 70.0
        )

        if not important and not resolver_override:
            log.info(
                "UPDATE rejected event=%s important=False "
                "score=%.1f resolver_relation=%s "
                "resolver_confidence=%.2f resolver_new_facts=%s "
                "reason=%s",
                eid,
                update_score,
                resolver_relation,
                score,
                resolver_new_facts,
                check.get("reason", ""),
            )
            log.info(
                "DUPLICATE event=%s resolver_confidence=%.3f "
                "update_score=%.1f",
                eid,
                score,
                update_score,
            )
            return

        if resolver_override and not important:
            log.info(
                "UPDATE resolver override event=%s "
                "score=%.1f resolver_relation=%s "
                "resolver_confidence=%.2f new_facts=%s",
                eid,
                update_score,
                resolver_relation,
                score,
                resolver_new_facts,
            )

        log.info(
            "UPDATE accepted event=%s score=%.1f "
            "resolver_relation=%s resolver_confidence=%.2f "
            "new_facts=%s important=%s; composing",
            eid,
            update_score,
            resolver_relation,
            score,
            resolver_new_facts,
            important,
        )

        try:
            data = await self.llm.compose_event_update(
                prev["title"] or "",
                prev["text"] or "",
                self.payload(r),
            )
        except Exception as exc:
            log.exception(
                "UPDATE compose failed event=%s error=%s",
                eid,
                exc,
            )
            return

        log.info(
            "UPDATE composed event=%s publish=%s confidence=%s "
            "title=%s reason=%s",
            eid,
            data.get("publish"),
            data.get("confidence"),
            data.get("title", ""),
            data.get("reason", ""),
        )

        if not self.s.auto_publish:
            log.warning(
                "UPDATE not published event=%s reason=auto_publish_disabled",
                eid,
            )
            return

        confidence = float(data.get("confidence", 0) or 0)

        if not data.get("publish"):
            log.info(
                "UPDATE not published event=%s "
                "reason=compose_publish_false confidence=%.2f",
                eid,
                confidence,
            )
            return

        if confidence < self.s.min_confidence_publish:
            log.warning(
                "UPDATE not published event=%s "
                "reason=confidence_below_threshold confidence=%.2f "
                "threshold=%.2f",
                eid,
                confidence,
                self.s.min_confidence_publish,
            )
            return

        log.info(
            "UPDATE ready event=%s confidence=%.2f",
            eid,
            confidence,
        )

        parent = self.db.last_publication(eid)
        if not parent:
            return

        parent_id = parent["id"]
        reply_to = parent["max_message_id"]

        # Explicitly record that this article is an UPGRADE.
        aid = self.db.add_article(
            eid,
            r["id"],
            "UPGRADE",
            data.get("title", ""),
            data.get("text", ""),
            float(data.get("confidence", 0)),
            data.get("reason", ""),
        )

        mid = await self.max.publish(
            data.get("title", ""),
            data.get("text", ""),
            "",
            reply_to=reply_to,
        )

        self.db.mark_published(
            eid,
            aid,
            mid,
            parent_id,
        )

        if self.telegram:
            await self.telegram.publish_article(
                eid,
                aid,
                "UPGRADE",
                data.get("title", ""),
                data.get("text", ""),
            )

        if self.threads:
            await self.threads.publish_article(
                eid,
                aid,
                "UPGRADE",
                data.get("title", ""),
                data.get("text", ""),
            )

        log.info(
            "UPGRADE event=%s max=%s reply_to=%s",
            eid,
            mid,
            reply_to,
        )

    async def publish_new(self, eid, aid, data):
        if not self.s.auto_publish:
            return

        if (
            int(data.get("priority", 0))
            < self.s.min_priority_publish
        ):
            return

        if (
            float(data.get("confidence", 0))
            < self.s.min_confidence_publish
        ):
            return

        mid = await self.max.publish(
            data.get("title", ""),
            data.get("text", ""),
        )

        self.db.mark_published(eid, aid, mid)

        if self.telegram:
            await self.telegram.publish_article(
                eid,
                aid,
                "NEW",
                data.get("title", ""),
                data.get("text", ""),
            )

        if self.threads:
            await self.threads.publish_article(
                eid,
                aid,
                "NEW",
                data.get("title", ""),
                data.get("text", ""),
            )

        log.info(
            "NEW event=%s max=%s",
            eid,
            mid,
        )
