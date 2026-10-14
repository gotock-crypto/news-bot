import logging
from .story_manager import StoryManager

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, s, db, llm, max_client, telegram_publisher=None, threads_publisher=None):
        self.s = s
        self.db = db
        self.llm = llm
        self.max = max_client
        self.telegram = telegram_publisher
        self.threads = threads_publisher
        self.story_manager = StoryManager(s, db, llm)

    def payload(self, r):
        return {"source": r["source"], "url": r["url"], "text": r["text"],
                "reliability": r["reliability"], "priority": r["priority"]}

    async def publish_threads(self, eid, aid, kind, title, text):
        if not self.threads:
            return

        result = await self.llm.compose_threads(
            title,
            text,
            kind=kind,
            max_length=getattr(self.s, "threads_max_length", 480),
        )

        if result.get("status") != "ok":
            log.warning(
                "THREADS skipped event=%s article=%s kind=%s reason=%s",
                eid,
                aid,
                kind,
                result.get("reason", ""),
            )
            return

        await self.threads.publish_article(
            eid,
            aid,
            kind,
            "",
            result["text"],
        )

    async def handle(self, msg_id, source):
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not r:
            raise ValueError(f"message {msg_id} not found")

        exact = self.db.exact_recent(
            r["norm_hash"],
            getattr(self.s, "dedup_hours", 24),
        )
        if exact and exact["id"] != r["id"]:
            rel = self.db.find_event_by_message(exact["id"])
            if rel:
                self.db.attach_event(rel["event_id"], msg_id, touch=False)
                log.info(
                    "DUPLICATE exact event=%s msg=%s",
                    rel["event_id"],
                    msg_id,
                )
                return

        data = await self.llm.edit([self.payload(r)])
        if not data.get("publish"):
            # Editorial rejection must not prevent Story identity resolution.
            # A non-publishable message can still be a duplicate/update of an
            # existing story and therefore must not create a second story.
            try:
                rejected_resolution = StoryManager.normalize_result(
                    await self.story_manager.classify(
                        data.get("title", ""), data.get("text", ""), data.get("category", "other"),
                        raw_title=r["text"][:200], raw_text=r["text"],
                    )
                )
            except Exception as exc:
                log.warning(
                    "MESSAGE rejected source=%s msg=%s story_check_failed=%s reason=%s",
                    source, msg_id, exc, data.get("reason", ""),
                )
                return

            rejected_relation = rejected_resolution["relation"]
            rejected_eid = rejected_resolution["story_id"]

            if rejected_relation in {"duplicate", "update"} and rejected_eid > 0:
                self.db.attach_event(rejected_eid, msg_id, touch=False)
                log.info(
                    "MESSAGE rejected but attached story=%s relation=%s source=%s msg=%s reason=%s",
                    rejected_eid,
                    rejected_relation,
                    source,
                    msg_id,
                    data.get("reason", ""),
                )
                return

            log.info(
                "MESSAGE rejected source=%s msg=%s story_relation=%s reason=%s",
                source,
                msg_id,
                rejected_relation,
                data.get("reason", ""),
            )
            return

        try:
            resolution = StoryManager.normalize_result(
                await self.story_manager.classify(
                    data.get("title", ""), data.get("text", ""), data.get("category", "other"),
                    raw_title=r["text"][:200], raw_text=r["text"],
                )
            )
        except Exception as exc:
            log.warning("STORY deferred msg=%s reason=%s", msg_id, exc)
            raise RuntimeError("story manager unavailable")

        relation = resolution["relation"]
        eid = resolution["story_id"]
        reason = resolution["reason"]

        if relation == "unknown":
            log.warning("MESSAGE deferred msg=%s reason=%s", msg_id, reason or "unknown")
            raise RuntimeError("story manager uncertain")

        if relation == "digest":
            log.info("MESSAGE rejected source=%s msg=%s story_relation=digest reason=%s", source, msg_id, reason)
            return

        if relation in {"duplicate", "update"} and eid <= 0:
            log.warning("MESSAGE deferred msg=%s reason=invalid_story_id", msg_id)
            raise RuntimeError("invalid story classification")

        if relation == "duplicate":
            self.db.attach_event(eid, msg_id, touch=True)
            log.info("STORY duplicate story=%s msg=%s reason=%s", eid, msg_id, reason)
            return

        if relation == "update":
            if not resolution["new_facts"]:
                log.warning("MESSAGE deferred msg=%s reason=update_without_new_fact", msg_id)
                raise RuntimeError("update without new fact")

            prev = self.db.last_publication(eid)
            if not prev:
                log.info("UPDATE skipped story=%s reason=no_previous_publication", eid)
                self.db.attach_event(eid, msg_id, touch=True)
                return

            known_facts = [
                row["fact_text"]
                for row in self.db.story_facts(eid, limit=30)
                if row["fact_text"]
            ]

            check = await self.llm.classify_update(
                prev["title"] or "",
                prev["text"] or "",
                self.payload(r),
                known_facts=known_facts,
            )

            if check.get("status") == "unavailable":
                log.warning("UPDATE deferred story=%s reason=classifier_unavailable", eid)
                raise RuntimeError("update classifier unavailable")

            score = float(check.get("score", 0) or 0)
            if score < 80:
                self.db.attach_event(eid, msg_id, touch=True)
                log.info(
                    "UPDATE skipped story=%s score=%.1f reason=%s",
                    eid,
                    score,
                    check.get("reason", ""),
                )
                return

            self.db.attach_event(eid, msg_id, touch=True)
            for fact in resolution["new_facts"]:
                self.db.add_story_fact(eid, fact, msg_id)

            await self.publish_story_update(eid, r, resolution)
            return

        # NEW is created only after semantic story classification explicitly chose NEW
        # and supplied at least one fact directly supported by the current RAW.
        if not resolution["new_facts"]:
            log.warning("MESSAGE deferred msg=%s reason=new_without_fact", msg_id)
            raise RuntimeError("new story without fact")
        eid = self.db.create_event(data["title"], data.get("category", "other"),
                                   int(data.get("priority", 0)), float(data.get("confidence", 0)), msg_id)
        for fact in resolution["new_facts"]:
            self.db.add_story_fact(eid, fact, msg_id)
        aid = self.db.add_article(eid, msg_id, "NEW", data["title"], data["text"],
                                  float(data.get("confidence", 0)), data.get("reason", ""))
        await self.publish_new(eid, aid, data)

    async def existing_event(self, eid, r):
        prev = self.db.last_publication(eid)
        if not prev:
            log.info("EVENT existing but no publication event=%s; no update", eid)
            return

        check = await self.llm.classify_update(prev["title"] or "", prev["text"] or "", self.payload(r))
        if check.get("status") == "unavailable":
            log.warning("UPDATE deferred event=%s reason=classifier_unavailable", eid)
            raise RuntimeError("update classifier unavailable")

        score = float(check.get("score", 0) or 0)
        if score < 80:
            log.info("UPDATE skipped event=%s score=%.1f reason=%s", eid, score, check.get("reason", ""))
            return

        data = await self.llm.compose_event_update(prev["title"] or "", prev["text"] or "", self.payload(r))
        if data.get("status") == "unavailable":
            log.warning("UPDATE deferred event=%s reason=compose_unavailable", eid)
            raise RuntimeError("update composer unavailable")
        if not data.get("publish"):
            log.info("UPDATE skipped event=%s reason=%s", eid, data.get("reason", ""))
            return

        confidence = float(data.get("confidence", 0) or 0)
        if confidence < self.s.min_confidence_publish:
            log.info("UPDATE skipped event=%s confidence=%.2f", eid, confidence)
            return

        parent = self.db.last_publication(eid)
        if not parent:
            return
        aid = self.db.add_article(eid, r["id"], "UPGRADE", data["title"], data["text"], confidence, data.get("reason", ""))
        mid = await self.max.publish(data["title"], data["text"], "", reply_to=parent["max_message_id"])
        self.db.mark_published(eid, aid, mid, parent["id"])
        if self.telegram:
            await self.telegram.publish_article(eid, aid, "UPGRADE", data["title"], data["text"])
        await self.publish_threads(
            eid,
            aid,
            "UPGRADE",
            data["title"],
            data["text"],
        )
        log.info("UPGRADE event=%s max=%s reply_to=%s score=%.1f", eid, mid, parent["max_message_id"], score)

    async def publish_story_update(self, eid, r, resolution):
        parent = self.db.last_publication(eid)
        if not parent:
            log.info("STORY update stored but no publication story=%s", eid)
            return
        title = ""
        text = " ".join(str(resolution.get("update_text", "")).split())
        if not text:
            log.info("STORY update skipped story=%s reason=no_new_fact_text", eid)
            return
        confidence = float(resolution.get("confidence", 0) or 0)
        if confidence < self.s.min_confidence_publish:
            log.info("STORY update skipped story=%s confidence=%.2f", eid, confidence)
            return
        if not self.s.auto_publish:
            log.info("STORY update skipped story=%s reason=auto_publish_disabled", eid)
            return
        aid = self.db.add_article(eid, r["id"], "UPGRADE", "", text, confidence, resolution.get("reason", ""))
        mid = await self.max.publish("", text, "", reply_to=parent["max_message_id"])
        self.db.mark_published(eid, aid, mid, parent["id"])
        if self.telegram:
            await self.telegram.publish_article(eid, aid, "UPGRADE", "", text)
        await self.publish_threads(
            eid,
            aid,
            "UPGRADE",
            "",
            text,
        )
        log.info("STORY UPDATE story=%s max=%s reply_to=%s", eid, mid, parent["max_message_id"])

    async def publish_new(self, eid, aid, data):
        if not self.s.auto_publish:
            log.info("NEW skipped event=%s reason=auto_publish_disabled", eid)
            return
        priority = int(data.get("priority", 0))
        confidence = float(data.get("confidence", 0) or 0)
        if priority < self.s.min_priority_publish:
            self.db.mark_rejected(eid, "priority_below_threshold")
            log.info("NEW rejected event=%s reason=priority", eid)
            return
        if confidence < self.s.min_confidence_publish:
            self.db.mark_rejected(eid, "confidence_below_threshold")
            log.info("NEW rejected event=%s reason=confidence", eid)
            return
        mid = await self.max.publish(data["title"], data["text"])
        self.db.mark_published(eid, aid, mid)
        if self.telegram:
            await self.telegram.publish_article(eid, aid, "NEW", data["title"], data["text"])
        await self.publish_threads(
            eid,
            aid,
            "NEW",
            data["title"],
            data["text"],
        )
        log.info("NEW event=%s max=%s", eid, mid)
