import sqlite3
import hashlib
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


class DB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init(self):
        with self.conn() as c:
            c.executescript(
                """
                PRAGMA journal_mode=WAL;

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
                    source_priority INTEGER DEFAULT 5,
                    source_reliability REAL DEFAULT 0.7,
                    source_category TEXT DEFAULT 'auto',
                    UNIQUE(source, source_message_id)
                );

                /*
                 * ВАЖНО:
                 * norm_hash НЕ должен быть UNIQUE.
                 *
                 * Разные Telegram-источники могут опубликовать
                 * одинаковый текст об одном событии. Нам необходимо
                 * сохранить оба сообщения, чтобы потом объединить
                 * их в одно news event.
                 */
                DROP INDEX IF EXISTS idx_messages_hash;
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
                    status TEXT DEFAULT 'new',
                    published_at TEXT,
                    max_message_id TEXT
                );

                CREATE TABLE IF NOT EXISTS event_messages(
                    event_id INTEGER,
                    message_id INTEGER,
                    UNIQUE(event_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS articles(
                    id INTEGER PRIMARY KEY,
                    event_id INTEGER UNIQUE,
                    text TEXT,
                    title TEXT,
                    category TEXT,
                    confidence REAL,
                    created_at TEXT NOT NULL,
                    status TEXT DEFAULT 'ready',
                    reason TEXT DEFAULT ''
                );

                /*
                 * Короткое окно семантического дедупа.
                 * Это НЕ архив и НЕ замена основной истории events/messages.
                 * Таблица содержит только события, которые были кандидатами
                 * на публикацию в последние N часов и автоматически очищается.
                 */
                CREATE TABLE IF NOT EXISTS event_window(
                    event_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    category TEXT DEFAULT 'other',
                    source TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_messages_created
                    ON messages(created_at);

                CREATE INDEX IF NOT EXISTS idx_event_status
                    ON events(status);

                CREATE INDEX IF NOT EXISTS idx_event_window_expires
                    ON event_window(expires_at);
                """
            )

            # Миграции для существующей БД.
            self._add_column(c, "messages", "source_priority", "INTEGER DEFAULT 5")
            self._add_column(c, "messages", "source_reliability", "REAL DEFAULT 0.7")
            self._add_column(c, "messages", "source_category", "TEXT DEFAULT 'auto'")
            self._add_column(c, "articles", "reason", "TEXT DEFAULT ''")

            self.cleanup_event_window_conn(c)

    @staticmethod
    def _add_column(c, table, column, definition):
        cols = {row[1] for row in c.execute(f"PRAGMA table_info({table}")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def cleanup_event_window_conn(c):
        now = datetime.now(timezone.utc).isoformat()
        c.execute("DELETE FROM event_window WHERE expires_at <= ?", (now,))

    @staticmethod
    def normalize(text):
        text = (text or "").lower().replace("ё", "е")
        text = re.sub(r"https?://\S+", " ", text)
        text = re.sub(r"[^\w\s]", " ", text, flags=re.U)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def sha(text):
        normalized = DB.normalize(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def insert_message(self, source, source_id, created_at, text, url="", media_path="", raw_json="", priority=5, reliability=0.7, category="auto"):
        h = self.sha(text)
        with self.conn() as c:
            try:
                cur = c.execute(
                    """
                    INSERT INTO messages(
                        source, source_message_id, created_at, text, url,
                        media_path, raw_json, norm_hash, source_priority,
                        source_reliability, source_category
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source, source_id, created_at, text, url, media_path, raw_json, h, priority, reliability, category),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" in str(exc):
                    return None
                raise

    def cleanup_retention(self, days=4):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        with self.conn() as c:
            c.execute("DELETE FROM event_messages WHERE message_id IN (SELECT id FROM messages WHERE created_at < ?)", (cutoff,))
            c.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            c.execute("DELETE FROM articles WHERE event_id NOT IN (SELECT id FROM events)")
            c.execute("DELETE FROM events WHERE id NOT IN (SELECT DISTINCT event_id FROM event_messages)")
            self.cleanup_event_window_conn(c)
        return cutoff

    def recent_messages(self, hours):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.conn() as c:
            return c.execute(
                "SELECT * FROM messages WHERE created_at >= ? ORDER BY created_at DESC LIMIT 2000",
                (since,),
            ).fetchall()

    def find_event_for_message(self, msg_id):
        with self.conn() as c:
            return c.execute(
                "SELECT event_id FROM event_messages WHERE message_id = ?",
                (msg_id,),
            ).fetchone()

    def create_event(self, title, category, priority, confidence, msg_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT INTO events(created_at, updated_at, title, category, priority, confidence)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (now, now, title, category, priority, confidence),
            )
            event_id = cur.lastrowid
            c.execute(
                "INSERT OR IGNORE INTO event_messages(event_id, message_id) VALUES(?, ?)",
                (event_id, msg_id),
            )
            return event_id

    def attach_event(self, event_id, msg_id):
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO event_messages(event_id, message_id) VALUES(?, ?)", (event_id, msg_id))
            c.execute("UPDATE events SET updated_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), event_id))

    def event_sources(self, event_id):
        with self.conn() as c:
            return c.execute(
                """
                SELECT DISTINCT m.source
                FROM messages m JOIN event_messages em ON em.message_id = m.id
                WHERE em.event_id = ? ORDER BY m.source
                """,
                (event_id,),
            ).fetchall()

    def event_messages(self, event_id):
        with self.conn() as c:
            return c.execute(
                """
                SELECT m.*
                FROM messages m JOIN event_messages em ON em.message_id = m.id
                WHERE em.event_id = ? ORDER BY m.created_at
                """,
                (event_id,),
            ).fetchall()

    def set_article(self, event_id, title, text, category, confidence, reason=""):
        with self.conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO articles(
                    event_id, text, title, category, confidence, created_at, status, reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, text, title, category, confidence, datetime.now(timezone.utc).isoformat(), "ready", reason),
            )

    def update_event(self, event_id, title, category, priority, confidence):
        with self.conn() as c:
            c.execute(
                """
                UPDATE events SET title=?, category=?, priority=?, confidence=?, updated_at=? WHERE id=?
                """,
                (title, category, priority, confidence, datetime.now(timezone.utc).isoformat(), event_id),
            )

    def mark_rejected(self, event_id, reason=""):
        with self.conn() as c:
            now = datetime.now(timezone.utc).isoformat()
            c.execute("UPDATE events SET status='rejected', updated_at=? WHERE id=?", (now, event_id))
            c.execute("UPDATE articles SET status='rejected', reason=? WHERE event_id=?", (reason, event_id))

    def ready_articles(self):
        with self.conn() as c:
            return c.execute(
                """
                SELECT a.*, e.status AS event_status, e.priority, e.max_message_id
                FROM articles a JOIN events e ON e.id = a.event_id
                WHERE a.status='ready' AND e.status='new'
                ORDER BY e.priority DESC, a.id ASC
                """
            ).fetchall()

    def mark_published(self, event_id, max_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            c.execute("UPDATE articles SET status='published' WHERE event_id=?", (event_id,))
            c.execute(
                """
                UPDATE events
                SET status='published', published_at=?, max_message_id=?, updated_at=?
                WHERE id=?
                """,
                (now, str(max_id), now, event_id),
            )

    # ------------------------------------------------------------------
    # Three-hour semantic event window
    # ------------------------------------------------------------------

    def event_window_candidates(self, hours, limit=6):
        since = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as c:
            c.execute("DELETE FROM event_window WHERE expires_at <= ?", (now,))
            return c.execute(
                """
                SELECT event_id, created_at, expires_at, title, text, category, source
                FROM event_window
                WHERE created_at >= ? AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since, now, int(limit)),
            ).fetchall()

    def upsert_event_window(self, event_id, title, text, category, source, hours):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=int(hours))
        with self.conn() as c:
            c.execute("DELETE FROM event_window WHERE expires_at <= ?", (now.isoformat(),))
            c.execute(
                """
                INSERT INTO event_window(event_id, created_at, expires_at, title, text, category, source)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    title=excluded.title,
                    text=excluded.text,
                    category=excluded.category,
                    source=excluded.source
                """,
                (event_id, now.isoformat(), expires.isoformat(), title or "", text or "", category or "other", source or ""),
            )

    def remove_event_window(self, event_id):
        with self.conn() as c:
            c.execute("DELETE FROM event_window WHERE event_id=?", (event_id,))
