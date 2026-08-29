import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

class ControlDB:
    def __init__(self, path, source_config):
        self.path=Path(path)
        self.source_config=Path(source_config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def conn(self):
        # Both services share the same WAL database.  Opening the DB can race
        # with SQLite/WAL initialization during simultaneous service restarts.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last=None
        for attempt in range(5):
            try:
                c=sqlite3.connect(self.path, timeout=30)
                c.row_factory=sqlite3.Row
                return c
            except sqlite3.OperationalError as exc:
                last=exc
                if attempt >= 4:
                    raise
                time.sleep(0.25*(attempt+1))
        raise last

    def init(self):
        with self.conn() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS managed_sources(
                username TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 5,
                category TEXT NOT NULL DEFAULT 'auto',
                reliability REAL NOT NULL DEFAULT 0.7,
                owner TEXT NOT NULL DEFAULT 'primary',
                telegram_entity_id INTEGER,
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_message_id INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_managed_sources_enabled ON managed_sources(enabled);
            CREATE TABLE IF NOT EXISTS admin_stats(
                key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0
            );
            """)
            # Silently block the unchanged primary collector from ingesting a source
            # that the admin panel disabled. Existing historical rows remain intact.
            has_messages = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
            if has_messages:
                c.executescript("""
                DROP TRIGGER IF EXISTS newsbot_admin_block_disabled_source;
                CREATE TRIGGER newsbot_admin_block_disabled_source
                BEFORE INSERT ON messages
                WHEN EXISTS (SELECT 1 FROM managed_sources ms WHERE ms.username = NEW.source AND ms.enabled = 0)
                BEGIN SELECT RAISE(IGNORE); END;
                """)
            self._seed_from_sources(c)

    def _seed_from_sources(self,c):
        if not self.source_config.exists(): return
        try: data=json.loads(self.source_config.read_text(encoding='utf-8'))
        except Exception: return
        now=datetime.now(timezone.utc).isoformat()
        for s in data.get('sources',[]):
            u=str(s.get('username','')).lstrip('@').lower()
            if not u: continue
            c.execute("""INSERT OR IGNORE INTO managed_sources
                (username,enabled,priority,category,reliability,owner,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (u,int(bool(s.get('enabled',True))),int(s.get('priority',5)),str(s.get('category','auto')),float(s.get('reliability',.7)),'primary',now,now))

    def list_sources(self, include_disabled=True):
        q="SELECT * FROM managed_sources" if include_disabled else "SELECT * FROM managed_sources WHERE enabled=1"
        with self.conn() as c: return c.execute(q+" ORDER BY enabled DESC, username").fetchall()

    def get_source(self,u):
        with self.conn() as c: return c.execute("SELECT * FROM managed_sources WHERE username=?",(u.lower(),)).fetchone()

    def upsert_source(self,u,priority=5,category='auto',reliability=.7,owner='addon',entity_id=None,title=None):
        now=datetime.now(timezone.utc).isoformat(); u=u.lstrip('@').lower()
        with self.conn() as c:
            existing=c.execute("SELECT owner,enabled FROM managed_sources WHERE username=?",(u,)).fetchone()
            if existing and owner == 'addon' and existing['owner'] == 'primary':
                raise ValueError('source already belongs to primary sources.json; use enable/disable instead')
            c.execute("""INSERT INTO managed_sources(username,enabled,priority,category,reliability,owner,telegram_entity_id,title,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET enabled=1,priority=excluded.priority,category=excluded.category,reliability=excluded.reliability,owner=excluded.owner,telegram_entity_id=COALESCE(excluded.telegram_entity_id,managed_sources.telegram_entity_id),title=COALESCE(excluded.title,managed_sources.title),updated_at=excluded.updated_at""",
            (u,1,priority,category,reliability,owner,entity_id,title,now,now))

    def disable_source(self,u):
        with self.conn() as c:
            cur=c.execute("UPDATE managed_sources SET enabled=0,updated_at=? WHERE username=?",(datetime.now(timezone.utc).isoformat(),u.lstrip('@').lower()))
            return cur.rowcount

    def enable_source(self,u):
        with self.conn() as c:
            cur=c.execute("UPDATE managed_sources SET enabled=1,updated_at=? WHERE username=?",(datetime.now(timezone.utc).isoformat(),u.lstrip('@').lower()))
            return cur.rowcount

    def delete_source(self,u):
        # Keep a disabled tombstone so the unchanged primary process cannot ingest it.
        return self.disable_source(u)

    def hot_sources(self):
        # The registry is now the single source of truth for the live control plane.
        # Primary sources are also monitored by HotWorker, while the unchanged
        # primary collector may continue running in parallel. SQLite's UNIQUE
        # (source, source_message_id) prevents duplicate ingestion.
        with self.conn() as c:
            return c.execute("SELECT * FROM managed_sources WHERE enabled=1 ORDER BY username").fetchall()

    def counts(self,hours=24):
        since=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        with self.conn() as c:
            received=c.execute("SELECT COUNT(*) n FROM messages WHERE created_at>=?",(since,)).fetchone()['n']
            events=c.execute("SELECT COUNT(*) n FROM events WHERE created_at>=?",(since,)).fetchone()['n']
            rejected=c.execute("SELECT COUNT(*) n FROM events WHERE created_at>=? AND status='rejected'",(since,)).fetchone()['n']
            published=c.execute("SELECT COUNT(*) n FROM events WHERE published_at>=? AND status='published'",(since,)).fetchone()['n']
            duplicates=c.execute("""SELECT COUNT(*) n FROM event_messages em JOIN events e ON e.id=em.event_id WHERE e.created_at>=?""",(since,)).fetchone()['n']
            return dict(received=received,events=events,rejected=rejected,published=published,linked_messages=duplicates)


    def source_stat(self, u, hours=24):
        since=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        with self.conn() as c:
            row=c.execute("""SELECT COUNT(*) received,
                SUM(CASE WHEN EXISTS(SELECT 1 FROM event_messages em WHERE em.message_id=m.id) THEN 1 ELSE 0 END) linked,
                MAX(m.created_at) last_received
                FROM messages m WHERE m.source=? AND m.created_at>=?""",(u.lstrip('@').lower(),since)).fetchone()
            return dict(row) if row else {'received':0,'linked':0,'last_received':None}

    def source_stats(self,hours=24):
        since=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        with self.conn() as c:
            return c.execute("""SELECT m.source, COUNT(*) received,
                SUM(CASE WHEN EXISTS(SELECT 1 FROM event_messages em WHERE em.message_id=m.id) THEN 1 ELSE 0 END) linked
                FROM messages m WHERE m.created_at>=? GROUP BY m.source ORDER BY received DESC""",(since,)).fetchall()
