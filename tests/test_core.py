from newsbot.db import DB
from newsbot.core.dedup import similarity,find_match
def test_dedup():
    assert similarity('Пожар в Москве','Пожар в Москве')==1.0
    assert find_match('В Москве произошел пожар',[{'id':1,'text':'В Москве произошел пожар'}],.86)[1]['id']==1
def test_db(tmp_path):
    db=DB(tmp_path/'news.db');db.upsert_source('test_news');assert db.get_source('test_news')['enabled']==1
    mid=db.insert_message('test_news',1,'2026-01-01T00:00:00+00:00','Тест');assert mid==1
    assert db.exact_recent(DB.sha('Тест'),99999)['id']==1
