#!/usr/bin/env python3
"""Read-only audit of active Story formation rules."""
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db',required=True); ap.add_argument('--hours',type=float,default=24); a=ap.parse_args()
    c=sqlite3.connect(a.db); c.row_factory=sqlite3.Row
    since=(datetime.now(timezone.utc)-timedelta(hours=a.hours)).isoformat()
    rows=c.execute('''SELECT e.id,e.title,e.status,e.created_at,e.updated_at,COUNT(em.message_id) messages,COUNT(sf.id) facts
                      FROM events e LEFT JOIN event_messages em ON em.event_id=e.id
                      LEFT JOIN story_facts sf ON sf.event_id=e.id
                      WHERE e.updated_at>=? GROUP BY e.id ORDER BY e.updated_at DESC,e.id DESC''',(since,)).fetchall()
    multi=[dict(r) for r in rows if r['messages']>1]
    nofacts=[dict(r) for r in rows if r['facts']==0]
    print('active_stories=',len(rows)); print('multi_message=',len(multi)); print('zero_fact=',len(nofacts))
    for r in multi[:100]: print(f"MULTI story={r['id']} messages={r['messages']} facts={r['facts']} status={r['status']} title={r['title']}")
    c.close()
if __name__=='__main__': main()
