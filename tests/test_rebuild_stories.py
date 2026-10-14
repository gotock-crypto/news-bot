import json
import sqlite3
from pathlib import Path

from newsbot.db import DB
from tools.rebuild_stories import apply_plan, validate_plan


def test_rebuild_plan_splits_messages_and_keeps_publication_owner(tmp_path):
    dbpath=tmp_path/'db.sqlite'; db=DB(dbpath)
    m1=db.insert_message('s',1,db.now(),'episode A')
    m2=db.insert_message('s',2,db.now(),'episode B')
    eid=db.create_event('mixed','world',5,.9,m1)
    db.attach_event(eid,m2)
    aid=db.add_article(eid,m1,'NEW','episode A','episode A',.9)
    db.mark_published(eid,aid,'max-1')
    plan={
      'format':'newsbot-story-rebuild-plan-v1','created_at':db.now(),
      'stories':[{'story_id':eid,'action':'split','groups':[
        {'keep_story_id':eid,'message_ids':[m1],'title':'episode A','facts':[{'fact_text':'episode A','source_message_id':m1}]},
        {'message_ids':[m2],'title':'episode B','facts':[{'fact_text':'episode B','source_message_id':m2}]}
      ]}]
    }
    assert validate_plan(db.conn(),plan)==[]
    result=apply_plan(db.conn(),plan)
    assert result['new_stories']==1
    c=sqlite3.connect(dbpath); c.row_factory=sqlite3.Row
    rows=c.execute('select id,title from events order by id').fetchall()
    assert len(rows)==2
    assert c.execute('select event_id from event_messages where message_id=?',(m1,)).fetchone()['event_id']==eid
    assert c.execute('select event_id from event_messages where message_id=?',(m2,)).fetchone()['event_id']!=eid
    assert c.execute('select count(*) n from publications where event_id=?',(eid,)).fetchone()['n']==1
    c.close()
