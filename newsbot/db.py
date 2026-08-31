import sqlite3
import hashlib
import re
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta


class DB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def normalize(text):
        text = (text or "").lower().replace("ё", "е")
        text = re.sub(r"https?://\S+", " ", text)
        text = re.sub(r"[^\w\s]", " ", text, flags=re.U)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def sha(cls, text):
        return hashlib.sha256(cls.normalize(text).encode()).hexdigest()

    def init(self):
        with self.conn() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS sources(
                username TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 5,
                category TEXT NOT NULL DEFAULT 'auto',
                reliability REAL NOT NULL DEFAULT .7,
                owner TEXT NOT NULL DEFAULT 'primary',
                telegram_entity_id INTEGER,
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                text TEXT NOT NULL,
                url TEXT,
                media_path TEXT,
                raw_json TEXT,
                norm_hash TEXT NOT NULL,
                priority INTEGER DEFAULT 5,
                reliability REAL DEFAULT .7,
                category TEXT DEFAULT 'auto',
                UNIQUE(source, source_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_time
                ON messages(created_at);

            CREATE INDEX IF NOT EXISTS idx_messages_hash
                ON messages(norm_hash);

            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                title TEXT,
                category TEXT,
                priority INTEGER DEFAULT 0,
                confidence REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new',
                published_at TEXT,
                last_max_message_id TEXT
            );

            CREATE TABLE IF NOT EXISTS event_messages(
                event_id INTEGER NOT NULL
                    REFERENCES events(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL
                    REFERENCES messages(id) ON DELETE CASCADE,
                UNIQUE(event_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS articles(
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL
                    REFERENCES events(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL
                    REFERENCES messages(id),
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                reason TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(event_id, kind, message_id)
            );

            CREATE TABLE IF NOT EXISTS publications(
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL
                    REFERENCES events(id) ON DELETE CASCADE,
                article_id INTEGER NOT NULL
                    REFERENCES articles(id),
                kind TEXT NOT NULL,
                max_message_id TEXT NOT NULL,
                parent_publication_id INTEGER
                    REFERENCES publications(id),
                created_at TEXT NOT NULL,
                UNIQUE(article_id)
            );

            CREATE TABLE IF NOT EXISTS admin_actions(
                id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                payload TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_state(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_destinations(
                id INTEGER PRIMARY KEY,
                chat_id TEXT NOT NULL UNIQUE,
                chat_type TEXT NOT NULL DEFAULT 'unknown',
                chat_title TEXT NOT NULL DEFAULT '',
                thread_id INTEGER,
                thread_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                configured_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tg_dest_enabled
                ON telegram_destinations(enabled);

            CREATE TABLE IF NOT EXISTS telegram_deliveries(
                id INTEGER PRIMARY KEY,
                destination_id INTEGER NOT NULL
                    REFERENCES telegram_destinations(id) ON DELETE CASCADE,
                article_id INTEGER NOT NULL
                    REFERENCES articles(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                telegram_message_id INTEGER,
                parent_delivery_id INTEGER
                    REFERENCES telegram_deliveries(id),
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(destination_id, article_id)
            );

            CREATE INDEX IF NOT EXISTS idx_tg_delivery_destination
                ON telegram_deliveries(destination_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_tg_delivery_article
                ON telegram_deliveries(article_id);
            """)

            self._migrate_legacy(c)

    def _migrate_legacy(self, c):
        """Keep the existing runtime/news.db usable without touching .env or sessions."""

        def cols(table):
            return {
                r[1]
                for r in c.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }

        if c.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='managed_sources'"
        ).fetchone():
            now = self.now()

            rows = c.execute(
                """
                SELECT
                    username,
                    enabled,
                    priority,
                    category,
                    reliability,
                    owner,
                    telegram_entity_id,
                    title,
                    created_at,
                    updated_at,
                    last_message_id
                FROM managed_sources
                """
            ).fetchall()

            for r in rows:
                c.execute(
                    """
                    INSERT INTO sources(
                        username,
                        enabled,
                        priority,
                        category,
                        reliability,
                        owner,
                        telegram_entity_id,
                        title,
                        created_at,
                        updated_at,
                        last_message_id
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(username) DO UPDATE SET
                        enabled=excluded.enabled,
                        priority=excluded.priority,
                        category=excluded.category,
                        reliability=excluded.reliability,
                        telegram_entity_id=
                            COALESCE(
                                excluded.telegram_entity_id,
                                sources.telegram_entity_id
                            ),
                        title=
                            COALESCE(
                                excluded.title,
                                sources.title
                            ),
                        last_message_id=
                            MAX(
                                sources.last_message_id,
                                excluded.last_message_id
                            ),
                        updated_at=excluded.updated_at
                    """,
                    (
                        r["username"],
                        r["enabled"],
                        r["priority"],
                        r["category"],
                        r["reliability"],
                        r["owner"],
                        r["telegram_entity_id"],
                        r["title"],
                        r["created_at"] or now,
                        r["updated_at"] or now,
                        r["last_message_id"] or 0,
                    ),
                )

        mc = cols("messages")

        for name, definition in [
            ("priority", "INTEGER DEFAULT 5"),
            ("reliability", "REAL DEFAULT .7"),
            ("category", "TEXT DEFAULT 'auto'"),
        ]:
            if name not in mc:
                c.execute(
                    f"ALTER TABLE messages ADD COLUMN {name} {definition}"
                )

        mc = cols("messages")

        if "source_priority" in mc:
            c.execute(
                """
                UPDATE messages
                SET priority=COALESCE(source_priority,5)
                WHERE priority IS NULL OR priority=5
                """
            )

        if "source_reliability" in mc:
            c.execute(
                """
                UPDATE messages
                SET reliability=COALESCE(source_reliability,.7)
                WHERE reliability IS NULL OR reliability=.7
                """
            )

        if "source_category" in mc:
            c.execute(
                """
                UPDATE messages
                SET category=COALESCE(source_category,'auto')
                WHERE category IS NULL OR category='auto'
                """
            )

        ec = cols("events")

        if "last_max_message_id" not in ec:
            c.execute(
                "ALTER TABLE events ADD COLUMN last_max_message_id TEXT"
            )

        ac = cols("articles")

        for name, definition in [
            ("message_id", "INTEGER"),
            ("kind", "TEXT DEFAULT 'NEW'"),
        ]:
            if name not in ac:
                c.execute(
                    f"ALTER TABLE articles ADD COLUMN {name} {definition}"
                )

        c.execute(
            """
            UPDATE articles
            SET kind='NEW'
            WHERE kind IS NULL OR kind=''
            """
        )

        c.execute(
            """
            UPDATE articles
            SET message_id=(
                SELECT em.message_id
                FROM event_messages em
                WHERE em.event_id=articles.event_id
                ORDER BY em.message_id
                LIMIT 1
            )
            WHERE message_id IS NULL
            """
        )

        c.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_articles_event_kind
            ON articles(event_id,kind)
            """
        )

    def seed_sources(self, sources):
        now = self.now()

        with self.conn() as c:
            for s in sources:
                c.execute(
                    """
                    INSERT INTO sources(
                        username,
                        enabled,
                        priority,
                        category,
                        reliability,
                        owner,
                        created_at,
                        updated_at
                    )
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(username) DO UPDATE SET
                        priority=excluded.priority,
                        category=excluded.category,
                        reliability=excluded.reliability
                    """,
                    (
                        s["username"],
                        int(s["enabled"]),
                        s["priority"],
                        s["category"],
                        s["reliability"],
                        s.get("owner", "primary"),
                        now,
                        now,
                    ),
                )

    def list_sources(self, enabled_only=False):
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM sources "
                + ("WHERE enabled=1 " if enabled_only else "")
                + "ORDER BY enabled DESC,username"
            ).fetchall()

    def get_source(self, u):
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM sources WHERE username=?",
                (u.lstrip("@").lower(),),
            ).fetchone()

    def upsert_source(
        self,
        u,
        priority=5,
        category="auto",
        reliability=.7,
        owner="addon",
    ):
        u = u.lstrip("@").lower()
        now = self.now()

        with self.conn() as c:
            c.execute(
                """
                INSERT INTO sources(
                    username,
                    enabled,
                    priority,
                    category,
                    reliability,
                    owner,
                    created_at,
                    updated_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                    enabled=1,
                    priority=excluded.priority,
                    category=excluded.category,
                    reliability=excluded.reliability,
                    owner=excluded.owner,
                    updated_at=excluded.updated_at
                """,
                (
                    u,
                    1,
                    priority,
                    category,
                    reliability,
                    owner,
                    now,
                    now,
                ),
            )

            c.execute(
                """
                INSERT INTO admin_actions(
                    action,
                    payload,
                    created_at
                )
                VALUES(?,?,?)
                """,
                (
                    "add_source",
                    json.dumps({"username": u}),
                    now,
                ),
            )

    def set_source(self, u, enabled):
        u = u.lstrip("@").lower()
        now = self.now()

        with self.conn() as c:
            n = c.execute(
                """
                UPDATE sources
                SET enabled=?,updated_at=?
                WHERE username=?
                """,
                (
                    int(enabled),
                    now,
                    u,
                ),
            ).rowcount

            c.execute(
                """
                INSERT INTO admin_actions(
                    action,
                    payload,
                    created_at
                )
                VALUES(?,?,?)
                """,
                (
                    "enable_source" if enabled else "disable_source",
                    json.dumps({"username": u}),
                    now,
                ),
            )

            return n

    def delete_source(self, u):
        u = u.lstrip("@").lower()
        now = self.now()

        with self.conn() as c:
            r = c.execute(
                "SELECT owner FROM sources WHERE username=?",
                (u,),
            ).fetchone()

            if not r:
                return 0

            if r["owner"] == "primary":
                return self.set_source(u, False)

            n = c.execute(
                "DELETE FROM sources WHERE username=?",
                (u,),
            ).rowcount

            c.execute(
                """
                INSERT INTO admin_actions(
                    action,
                    payload,
                    created_at
                )
                VALUES(?,?,?)
                """,
                (
                    "delete_source",
                    json.dumps({"username": u}),
                    now,
                ),
            )

            return n

    def update_watermark(self, u, msg_id):
        with self.conn() as c:
            c.execute(
                """
                UPDATE sources
                SET last_message_id=?,updated_at=?
                WHERE username=? AND last_message_id<?
                """,
                (
                    int(msg_id),
                    self.now(),
                    u.lstrip("@").lower(),
                    int(msg_id),
                ),
            )

    def insert_message(
        self,
        source,
        source_id,
        created_at,
        text,
        url="",
        media_path="",
        raw_json="",
        priority=5,
        reliability=.7,
        category="auto",
    ):
        with self.conn() as c:
            try:
                return c.execute(
                    """
                    INSERT INTO messages(
                        source,
                        source_message_id,
                        created_at,
                        text,
                        url,
                        media_path,
                        raw_json,
                        norm_hash,
                        priority,
                        reliability,
                        category
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        source,
                        int(source_id),
                        created_at,
                        text or "",
                        url or "",
                        media_path or "",
                        raw_json or "",
                        self.sha(text),
                        priority,
                        reliability,
                        category,
                    ),
                ).lastrowid

            except sqlite3.IntegrityError:
                r = c.execute(
                    """
                    SELECT id
                    FROM messages
                    WHERE source=? AND source_message_id=?
                    """,
                    (
                        source,
                        int(source_id),
                    ),
                ).fetchone()

                return r["id"] if r else None

    def recent_messages(self, hours=6, limit=5000):
        since = (
            datetime.now(timezone.utc)
            - timedelta(hours=hours)
        ).isoformat()

        with self.conn() as c:
            return c.execute(
                """
                SELECT *
                FROM messages
                WHERE created_at>=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (
                    since,
                    limit,
                ),
            ).fetchall()

    def exact_recent(self, h, hours=6):
        since = (
            datetime.now(timezone.utc)
            - timedelta(hours=hours)
        ).isoformat()

        with self.conn() as c:
            return c.execute(
                """
                SELECT *
                FROM messages
                WHERE norm_hash=? AND created_at>=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    h,
                    since,
                ),
            ).fetchone()

    def create_event(
        self,
        title,
        category,
        priority,
        confidence,
        msg_id,
    ):
        now = self.now()

        with self.conn() as c:
            eid = c.execute(
                """
                INSERT INTO events(
                    created_at,
                    updated_at,
                    title,
                    category,
                    priority,
                    confidence
                )
                VALUES(?,?,?,?,?,?)
                """,
                (
                    now,
                    now,
                    title,
                    category,
                    priority,
                    confidence,
                ),
            ).lastrowid

            c.execute(
                """
                INSERT INTO event_messages(
                    event_id,
                    message_id
                )
                VALUES(?,?)
                """,
                (
                    eid,
                    msg_id,
                ),
            )

            return eid

    def attach_event(self, eid, msg_id):
        with self.conn() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO event_messages(
                    event_id,
                    message_id
                )
                VALUES(?,?)
                """,
                (
                    eid,
                    msg_id,
                ),
            )

            c.execute(
                """
                UPDATE events
                SET updated_at=?
                WHERE id=?
                """,
                (
                    self.now(),
                    eid,
                ),
            )

    def find_event_by_message(self, msg_id):
        with self.conn() as c:
            return c.execute(
                """
                SELECT event_id
                FROM event_messages
                WHERE message_id=?
                """,
                (msg_id,),
            ).fetchone()

    def get_article(self, eid, kind):
        with self.conn() as c:
            return c.execute(
                """
                SELECT *
                FROM articles
                WHERE event_id=? AND kind=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    eid,
                    kind,
                ),
            ).fetchone()

    def add_article(
        self,
        eid,
        msg_id,
        kind,
        title,
        text,
        confidence,
        reason="",
    ):
        with self.conn() as c:
            return c.execute(
                """
                INSERT INTO articles(
                    event_id,
                    message_id,
                    kind,
                    title,
                    text,
                    confidence,
                    reason,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    eid,
                    msg_id,
                    kind,
                    title,
                    text,
                    confidence,
                    reason,
                    self.now(),
                ),
            ).lastrowid

    def mark_rejected(self, eid, reason):
        with self.conn() as c:
            c.execute(
                """
                UPDATE events
                SET status="rejected",
                    updated_at=?
                WHERE id=?
                """,
                (
                    self.now(),
                    eid,
                ),
            )

    def last_publication(self, eid):
        with self.conn() as c:
            return c.execute(
                """
                SELECT
                    p.*,
                    a.title,
                    a.text,
                    a.kind
                FROM publications p
                JOIN articles a ON a.id=p.article_id
                WHERE p.event_id=?
                ORDER BY p.id DESC
                LIMIT 1
                """,
                (eid,),
            ).fetchone()

    def mark_published(
        self,
        eid,
        article_id,
        max_id,
        parent_id=None,
    ):
        now = self.now()

        with self.conn() as c:
            pid = c.execute(
                """
                INSERT INTO publications(
                    event_id,
                    article_id,
                    kind,
                    max_message_id,
                    parent_publication_id,
                    created_at
                )
                SELECT
                    ?,
                    ?,
                    kind,
                    ?,
                    ?,
                    ?
                FROM articles
                WHERE id=?
                """,
                (
                    eid,
                    article_id,
                    str(max_id),
                    parent_id,
                    now,
                    article_id,
                ),
            ).lastrowid

            c.execute(
                """
                UPDATE events
                SET
                    status="published",
                    published_at=COALESCE(published_at,?),
                    last_max_message_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    now,
                    str(max_id),
                    now,
                    eid,
                ),
            )

            return pid

    def stats(self, hours=24):
        since = (
            datetime.now(timezone.utc)
            - timedelta(hours=hours)
        ).isoformat()

        with self.conn() as c:
            return {
                "received": c.execute(
                    """
                    SELECT COUNT(*) n
                    FROM messages
                    WHERE created_at>=?
                    """,
                    (since,),
                ).fetchone()["n"],

                "events": c.execute(
                    """
                    SELECT COUNT(*) n
                    FROM events
                    WHERE created_at>=?
                    """,
                    (since,),
                ).fetchone()["n"],

                "upgrades": c.execute(
                    """
                    SELECT COUNT(*) n
                    FROM articles
                    WHERE kind='UPGRADE'
                    AND created_at>=?
                    """,
                    (since,),
                ).fetchone()["n"],

                "published": c.execute(
                    """
                    SELECT COUNT(*) n
                    FROM publications
                    WHERE created_at>=?
                    """,
                    (since,),
                ).fetchone()["n"],
            }

    def source_stats(self, hours=24):
        since = (
            datetime.now(timezone.utc)
            - timedelta(hours=hours)
        ).isoformat()

        with self.conn() as c:
            return c.execute(
                """
                SELECT
                    source,
                    COUNT(*) received,
                    MAX(created_at) last_received
                FROM messages
                WHERE created_at>=?
                GROUP BY source
                ORDER BY received DESC
                """,
                (since,),
            ).fetchall()

    # ------------------------------------------------------------------
    # Telegram publication destinations
    # ------------------------------------------------------------------
    def upsert_telegram_destination(
        self,
        chat_id,
        chat_type,
        chat_title,
        thread_id=None,
        thread_name="",
        configured_by=None,
    ):
        now = self.now()
        chat_id = str(chat_id)
        with self.conn() as c:
            r = c.execute(
                "SELECT id FROM telegram_destinations WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if r:
                c.execute(
                    """
                    UPDATE telegram_destinations
                    SET chat_type=?, chat_title=?, thread_id=?, thread_name=?,
                        enabled=1, configured_by=COALESCE(?, configured_by),
                        updated_at=?
                    WHERE id=?
                    """,
                    (chat_type, chat_title or "", thread_id, thread_name or "",
                     configured_by, now, r["id"]),
                )
                return int(r["id"])
            return c.execute(
                """
                INSERT INTO telegram_destinations(
                    chat_id, chat_type, chat_title, thread_id, thread_name,
                    enabled, configured_by, created_at, updated_at
                ) VALUES(?,?,?,?,?,1,?,?,?)
                """,
                (chat_id, chat_type, chat_title or "", thread_id,
                 thread_name or "", configured_by, now, now),
            ).lastrowid

    def list_telegram_destinations(self, enabled_only=False):
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM telegram_destinations "
                + ("WHERE enabled=1 " if enabled_only else "")
                + "ORDER BY enabled DESC, chat_title, id"
            ).fetchall()

    def get_telegram_destination(self, destination_id):
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM telegram_destinations WHERE id=?",
                (int(destination_id),),
            ).fetchone()

    def set_telegram_destination(self, destination_id, enabled):
        with self.conn() as c:
            return c.execute(
                "UPDATE telegram_destinations SET enabled=?, updated_at=? WHERE id=?",
                (int(bool(enabled)), self.now(), int(destination_id)),
            ).rowcount

    def delete_telegram_destination(self, destination_id):
        with self.conn() as c:
            return c.execute(
                "DELETE FROM telegram_destinations WHERE id=?",
                (int(destination_id),),
            ).rowcount

    def telegram_delivery(self, destination_id, article_id):
        with self.conn() as c:
            return c.execute(
                """
                SELECT * FROM telegram_deliveries
                WHERE destination_id=? AND article_id=?
                """,
                (int(destination_id), int(article_id)),
            ).fetchone()

    def last_telegram_delivery(self, destination_id, event_id=None):
        with self.conn() as c:
            if event_id is None:
                return c.execute(
                    """
                    SELECT td.*, a.event_id
                    FROM telegram_deliveries td
                    JOIN articles a ON a.id=td.article_id
                    WHERE td.destination_id=? AND td.status='sent'
                    ORDER BY td.id DESC LIMIT 1
                    """,
                    (int(destination_id),),
                ).fetchone()
            return c.execute(
                """
                SELECT td.*, a.event_id
                FROM telegram_deliveries td
                JOIN articles a ON a.id=td.article_id
                WHERE td.destination_id=? AND a.event_id=?
                  AND td.status='sent'
                ORDER BY td.id DESC LIMIT 1
                """,
                (int(destination_id), int(event_id)),
            ).fetchone()

    def record_telegram_delivery(
        self, destination_id, article_id, kind, status,
        telegram_message_id=None, parent_delivery_id=None, error="",
    ):
        now = self.now()
        with self.conn() as c:
            existing = c.execute(
                "SELECT id FROM telegram_deliveries WHERE destination_id=? AND article_id=?",
                (int(destination_id), int(article_id)),
            ).fetchone()
            if existing:
                c.execute(
                    """
                    UPDATE telegram_deliveries
                    SET kind=?, telegram_message_id=?, parent_delivery_id=?,
                        status=?, error=?, updated_at=?
                    WHERE id=?
                    """,
                    (kind, telegram_message_id, parent_delivery_id, status, error or "",
                     now, existing["id"]),
                )
                return int(existing["id"])
            return c.execute(
                """
                INSERT INTO telegram_deliveries(
                    destination_id, article_id, kind, telegram_message_id,
                    parent_delivery_id, status, error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (int(destination_id), int(article_id), kind, telegram_message_id,
                 parent_delivery_id, status, error or "", now, now),
            ).lastrowid

    def telegram_stats(self, hours=24):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.conn() as c:
            total = c.execute(
                "SELECT COUNT(*) n FROM telegram_deliveries WHERE created_at>=?",
                (since,),
            ).fetchone()["n"]
            sent = c.execute(
                "SELECT COUNT(*) n FROM telegram_deliveries WHERE created_at>=? AND status='sent'",
                (since,),
            ).fetchone()["n"]
            failed = c.execute(
                "SELECT COUNT(*) n FROM telegram_deliveries WHERE created_at>=? AND status='failed'",
                (since,),
            ).fetchone()["n"]
            enabled = c.execute(
                "SELECT COUNT(*) n FROM telegram_destinations WHERE enabled=1"
            ).fetchone()["n"]
            destinations = c.execute(
                """
                SELECT d.*,
                       SUM(CASE WHEN td.status='sent' AND td.created_at>=? THEN 1 ELSE 0 END) sent,
                       SUM(CASE WHEN td.status='failed' AND td.created_at>=? THEN 1 ELSE 0 END) failed,
                       MAX(CASE WHEN td.status='sent' THEN td.updated_at END) last_sent
                FROM telegram_destinations d
                LEFT JOIN telegram_deliveries td ON td.destination_id=d.id
                GROUP BY d.id
                ORDER BY d.enabled DESC, d.chat_title, d.id
                """,
                (since, since),
            ).fetchall()
            return {
                "destinations": destinations,
                "total": total,
                "sent": sent,
                "failed": failed,
                "enabled": enabled,
            }

    def set_state(self, k, v):
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO system_state(
                    key,
                    value,
                    updated_at
                )
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (
                    k,
                    str(v),
                    self.now(),
                ),
            )

    def get_state(self, k, d=""):
        with self.conn() as c:
            r = c.execute(
                """
                SELECT value
                FROM system_state
                WHERE key=?
                """,
                (k,),
            ).fetchone()

            return r["value"] if r else d

    def cleanup(self, days):
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=days)
        ).isoformat()

        with self.conn() as c:
            # articles.message_id -> messages.id uses NO ACTION,
            # so remove dependent rows before removing messages.
            c.execute(
                """
                DELETE FROM telegram_deliveries
                WHERE article_id IN (
                    SELECT a.id
                    FROM articles a
                    JOIN messages m ON m.id=a.message_id
                    WHERE m.created_at<?
                )
                """,
                (cutoff,),
            )

            c.execute(
                """
                DELETE FROM publications
                WHERE article_id IN (
                    SELECT a.id
                    FROM articles a
                    JOIN messages m ON m.id=a.message_id
                    WHERE m.created_at<?
                )
                """,
                (cutoff,),
            )

            c.execute(
                """
                DELETE FROM articles
                WHERE message_id IN (
                    SELECT id
                    FROM messages
                    WHERE created_at<?
                )
                """,
                (cutoff,),
            )

            c.execute(
                """
                DELETE FROM messages
                WHERE created_at<?
                """,
                (cutoff,),
            )
