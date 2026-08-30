import logging

from .dedup import find_match
from .event_resolver import EventResolver

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, s, db, llm, max_client):
        self.s = s
        self.db = db
        self.llm = llm
        self.max = max_client
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
        # 2. Strong lexical duplicate.
        # ---------------------------------------------------------
        recent = [
            x
            for x in self.db.recent_messages(
                getattr(self.s, "dedup_hours", 6)
            )
            if x["id"] != r["id"]
        ]

        match = find_match(
            r["text"],
            recent,
            float(getattr(self.s, "similarity_threshold", 0.72)),
        )

        if match:
            rel = self.db.find_event_by_message(match[1]["id"])

            # IMPORTANT:
            # A rejected event must never absorb a new message.
            if rel:
                with self.db.conn() as c:
                    event = c.execute(
                        "SELECT status FROM events WHERE id=?",
                        (rel["event_id"],),
                    ).fetchone()

                if event and event["status"] != "rejected":
                    self.db.attach_event(rel["event_id"], msg_id)
                    await self.existing_event(
                        rel["event_id"],
                        r,
                        match[0],
                    )
                    return

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

        if relation == "duplicate" and event_id:
            self.db.attach_event(event_id, msg_id)
            log.info(
                "DUPLICATE semantic event=%s msg=%s confidence=%.2f",
                event_id,
                msg_id,
                float(resolution.get("confidence", 0)),
            )
            return

        if relation == "update" and event_id:
            self.db.attach_event(event_id, msg_id)
            await self.existing_event(
                event_id,
                r,
                float(resolution.get("confidence", 0)),
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

    async def existing_event(self, eid, r, score=0.0):
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

        if not check.get("important"):
            log.info(
                "DUPLICATE event=%s similarity=%.3f",
                eid,
                score,
            )
            return

        data = await self.llm.compose_event_update(
            prev["title"] or "",
            prev["text"] or "",
            self.payload(r),
        )

        if not self.s.auto_publish:
            return

        if (
            not data.get("publish")
            or float(data.get("confidence", 0))
            < self.s.min_confidence_publish
        ):
            return

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
        log.info(
            "NEW event=%s max=%s",
            eid,
            mid,
        )
