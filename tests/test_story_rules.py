import asyncio
from types import SimpleNamespace

from newsbot.db import DB
from newsbot.core.pipeline import Pipeline


class LLM:
    def __init__(self, result): self.result = result
    async def edit(self, sources):
        return {"publish": True, "title": "Черновик", "text": "Черновик", "category": "world",
                "priority": 5, "confidence": .95, "reason": "", "hashtags": []}
    async def classify_story(self, context):
        self.context = context
        return self.result


class Max:
    def __init__(self): self.calls=[]
    async def publish(self, title, text, *args, **kwargs):
        self.calls.append((title,text,kwargs)); return "m1"


def settings():
    return SimpleNamespace(auto_publish=True, min_priority_publish=0, min_confidence_publish=.8,
                           dedup_hours=24, story_hours=24, story_candidate_limit=60,
                           story_semantic_candidates=8)


def test_different_episode_with_same_person_is_new(tmp_path):
    db=DB(tmp_path/'db.sqlite')
    m1=db.insert_message('s',1,db.now(),'Медведев призвал ударить по производствам Германии')
    eid=db.create_event('Удар по производствам Германии', 'world', 5,.95,m1)
    db.add_story_fact(eid,'Медведев призвал ударить по производствам Германии',m1)
    m2=db.insert_message('s',2,db.now(),'Медведев заявил о возможном ответе на угрозы Киева')
    llm=LLM({"relation":"new","story_id":0,"confidence":.93,
             "new_facts":["Медведев заявил о возможном ответе на угрозы Киева"],
             "update_title":"","update_text":"","reason":"другой конкретный эпизод"})
    p=Pipeline(settings(),db,llm,Max())
    asyncio.run(p.handle(m2,'s'))
    with db.conn() as c:
        assert c.execute('select count(*) n from events').fetchone()['n']==2


def test_update_without_new_fact_is_not_accepted(tmp_path):
    db=DB(tmp_path/'db.sqlite')
    m1=db.insert_message('s',1,db.now(),'Путин прибыл в Казахстан')
    eid=db.create_event('Путин в Казахстане','world',5,.95,m1)
    db.add_story_fact(eid,'Путин прибыл в Казахстан',m1)
    m2=db.insert_message('s',2,db.now(),'Путин приехал в Казахстан')
    llm=LLM({"relation":"update","story_id":eid,"confidence":.95,
             "new_facts":[],"update_title":"","update_text":"","reason":"нет нового факта"})
    try:
        asyncio.run(Pipeline(settings(),db,llm,Max()).handle(m2,'s'))
    except RuntimeError:
        pass
    else:
        raise AssertionError('update without new fact must be deferred')
