import asyncio
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger("threads")


class ThreadsPublisher:
    """Best-effort Threads publisher isolated from MAX/Telegram."""

    def __init__(self, settings, db):
        self.settings = settings
        self.db = db

        self.enabled = bool(
            getattr(settings, "threads_enabled", False)
        )
        self.dry_run = bool(
            getattr(settings, "threads_dry_run", True)
        )
        self.token = getattr(
            settings, "threads_access_token", ""
        )

        self.base_url = getattr(
            settings,
            "threads_api_base_url",
            "https://graph.threads.net",
        ).rstrip("/")

        self.max_length = max(
            100,
            min(
                int(
                    getattr(
                        settings,
                        "threads_max_length",
                        480,
                    )
                ),
                500,
            ),
        )

        self.http = requests.Session()
        self._init_db()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _clean(value):
        return " ".join((value or "").split()).strip()

    def _init_db(self):
        with self.db.conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS threads_deliveries(
                    id INTEGER PRIMARY KEY,
                    event_id INTEGER NOT NULL,
                    article_id INTEGER NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    threads_post_id TEXT,
                    parent_delivery_id INTEGER,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            c.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_threads_deliveries_event
                ON threads_deliveries(event_id)
                """
            )

    def _existing(self, article_id):
        with self.db.conn() as c:
            return c.execute(
                """
                SELECT *
                FROM threads_deliveries
                WHERE article_id=?
                """,
                (article_id,),
            ).fetchone()

    def _previous_sent(self, event_id, article_id):
        with self.db.conn() as c:
            return c.execute(
                """
                SELECT *
                FROM threads_deliveries
                WHERE event_id=?
                  AND article_id<>?
                  AND status='sent'
                ORDER BY id DESC
                LIMIT 1
                """,
                (event_id, article_id),
            ).fetchone()

    def _record(
        self,
        event_id,
        article_id,
        kind,
        text,
        status,
        threads_post_id=None,
        parent_delivery_id=None,
        error="",
    ):
        now = self._now()

        with self.db.conn() as c:
            c.execute(
                """
                INSERT INTO threads_deliveries(
                    event_id,
                    article_id,
                    kind,
                    threads_post_id,
                    parent_delivery_id,
                    text,
                    status,
                    error,
                    created_at,
                    updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(article_id) DO UPDATE SET
                    kind=excluded.kind,
                    threads_post_id=excluded.threads_post_id,
                    parent_delivery_id=excluded.parent_delivery_id,
                    text=excluded.text,
                    status=excluded.status,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    article_id,
                    kind,
                    threads_post_id,
                    parent_delivery_id,
                    text,
                    status,
                    error[:1000],
                    now,
                    now,
                ),
            )

    def format_text(self, title, text, kind="NEW"):
        title = self._clean(title)
        text = self._clean(text)

        prefix = "UPDATE: " if kind == "UPGRADE" else ""

        if title and text:
            result = f"{prefix}{title}\n\n{text}"
        else:
            result = f"{prefix}{title or text}"

        if len(result) <= self.max_length:
            return result

        if title:
            head = f"{prefix}{title}\n\n"
            available = self.max_length - len(head)

            if available > 40:
                body = text[:available].rstrip()

                if " " in body:
                    body = body.rsplit(" ", 1)[0]

                result = head + body.rstrip(" .,;:-")

        if len(result) > self.max_length:
            result = result[: self.max_length].rstrip()

        return result

    def _post_sync(self, text, reply_to_id=None):
        if not self.token:
            raise RuntimeError(
                "THREADS_ACCESS_TOKEN is not configured"
            )

        headers = {
            "Authorization": f"Bearer {self.token}",
        }

        params = {
            "media_type": "TEXT",
            "text": text,
        }

        if reply_to_id:
            params["reply_to_id"] = str(reply_to_id)

        create = self.http.post(
            f"{self.base_url}/me/threads",
            params=params,
            headers=headers,
            timeout=30,
        )
        create.raise_for_status()

        data = create.json()
        creation_id = data.get("id")

        if not creation_id:
            raise RuntimeError(
                f"Threads container response has no id: {data}"
            )

        publish = self.http.post(
            f"{self.base_url}/me/threads_publish",
            params={
                "creation_id": creation_id,
            },
            headers=headers,
            timeout=30,
        )
        publish.raise_for_status()

        data = publish.json()
        post_id = data.get("id")

        if not post_id:
            raise RuntimeError(
                f"Threads publish response has no id: {data}"
            )

        return str(post_id)

    async def publish_article(
        self,
        eid,
        article_id,
        kind,
        title,
        text,
    ):
        if not self.enabled:
            return

        rendered = self.format_text(
            title,
            text,
            kind,
        )

        if not rendered:
            log.warning(
                "THREADS skip empty article=%s",
                article_id,
            )
            return

        if len(rendered) > self.max_length:
            log.error(
                "THREADS formatter exceeded limit "
                "article=%s length=%s limit=%s",
                article_id,
                len(rendered),
                self.max_length,
            )
            return

        existing = self._existing(article_id)

        if existing and existing["status"] == "sent":
            log.info(
                "THREADS already sent article=%s post_id=%s",
                article_id,
                existing["threads_post_id"],
            )
            return

        parent = None

        if kind == "UPGRADE":
            parent = self._previous_sent(
                eid,
                article_id,
            )

        parent_post_id = (
            parent["threads_post_id"]
            if parent
            else None
        )

        parent_delivery_id = (
            parent["id"]
            if parent
            else None
        )

        if self.dry_run:
            log.info(
                "THREADS DRY_RUN event=%s article=%s "
                "kind=%s length=%s reply_to=%s text=%r",
                eid,
                article_id,
                kind,
                len(rendered),
                parent_post_id,
                rendered,
            )

            self._record(
                eid,
                article_id,
                kind,
                rendered,
                "dry_run",
                parent_delivery_id=parent_delivery_id,
            )
            return

        try:
            post_id = await asyncio.to_thread(
                self._post_sync,
                rendered,
                parent_post_id,
            )

            self._record(
                eid,
                article_id,
                kind,
                rendered,
                "sent",
                threads_post_id=post_id,
                parent_delivery_id=parent_delivery_id,
            )

            log.info(
                "THREADS published event=%s article=%s "
                "kind=%s post_id=%s reply_to=%s length=%s",
                eid,
                article_id,
                kind,
                post_id,
                parent_post_id,
                len(rendered),
            )

        except Exception as exc:
            self._record(
                eid,
                article_id,
                kind,
                rendered,
                "failed",
                parent_delivery_id=parent_delivery_id,
                error=str(exc),
            )

            log.exception(
                "THREADS publication failed event=%s "
                "article=%s kind=%s error=%s",
                eid,
                article_id,
                kind,
                exc,
            )

            # Deliberately swallowed.
            # Threads must never break MAX/Telegram.

    async def close(self):
        try:
            self.http.close()
        except Exception:
            pass
