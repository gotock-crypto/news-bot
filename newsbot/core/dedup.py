import re
from difflib import SequenceMatcher
STOP=set('и в во на по из за от до с со а но что как это уже еще или для при не ни был была были быть есть мы вы они он она оно их его ее где когда кто почему чтобы о об же бы ли то так этот эта эти тот та те'.split())
def tokens(text): return {x for x in re.findall(r'[а-яa-z0-9]{3,}',(text or '').lower()) if x not in STOP}
def similarity(a,b):
    ta,tb=tokens(a),tokens(b)
    if not ta or not tb:return 0.0
    j=len(ta&tb)/max(1,len(ta|tb)); seq=SequenceMatcher(None,a.lower(),b.lower()).ratio()
    return max(j,seq*.92)
def find_similar(text,rows,threshold):
    best=None
    for r in rows:
        s=similarity(text,r['text'])
        if s>=threshold and (best is None or s>best[0]): best=(s,r)
    return best
