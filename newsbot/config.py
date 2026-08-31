import json, os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env')
def env(n,d=''): return os.getenv(n,d).strip()
def env_int(n,d=0):
    try:return int(env(n,str(d)))
    except ValueError:return d
def env_float(n,d=0.0):
    try:return float(env(n,str(d)))
    except ValueError:return d
@dataclass(frozen=True)
class Settings:
    tg_api_id:int; tg_api_hash:str; tg_phone:str; tg_session:str
    max_phone:str; max_session:str; max_work_dir:str; max_channel_id:str
    groq_key:str; groq_model:str; groq_max_tokens:int; groq_temperature:float
    gigachat_key:str; gigachat_model:str; gigachat_max_tokens:int; gigachat_temperature:float
    mistral_key:str; mistral_model:str; mistral_max_tokens:int; mistral_temperature:float
    llm_timeout:int; llm_min_interval_seconds:float; groq_cooldown_seconds:float
    db_file:str; source_config:str; auto_publish:bool; poll_interval:int; dedup_hours:int; similarity_threshold:float
    event_resolver_hours:float; event_resolver_max_candidates:int; event_resolver_similarity_floor:float; event_resolver_confidence:float; event_resolver_min_interval_seconds:float; event_resolver_scan_limit:int; event_resolver_fallback_similarity:float
    max_post_length:int; retention_days:int; log_level:str; min_priority_publish:int; min_confidence_publish:float
    admin_bot_token:str; admin_ids:tuple[int,...]; admin_poll_seconds:int
    @property
    def db_path(self): return ROOT/self.db_file
    @property
    def source_path(self): return ROOT/self.source_config
    @property
    def max_dir(self): return ROOT/self.max_work_dir
    @property
    def tg_session_path(self): return str(ROOT/self.tg_session)
def load_settings():
    ids=[]
    for x in env('ADMIN_IDS').split(','):
        if x.strip():
            try: ids.append(int(x.strip()))
            except ValueError: pass
    return Settings(
        env_int('TG_API_ID'),env('TG_API_HASH'),env('TG_PHONE'),env('TG_SESSION','runtime/telegram/newsbot'),
        env('MAX_PHONE'),env('MAX_SESSION','max.db'),env('MAX_WORK_DIR','runtime/max'),env('MAX_CHANNEL_ID'),
        env('GROQ_API_KEY'),env('GROQ_MODEL','qwen/qwen3-27b'),env_int('GROQ_MAX_TOKENS',1800),env_float('GROQ_TEMPERATURE',.35),
        env('GIGACHAT_API_KEY'),env('GIGACHAT_MODEL','GigaChat-2'),env_int('GIGACHAT_MAX_TOKENS',600),env_float('GIGACHAT_TEMPERATURE',.35),
        env('MISTRAL_API_KEY'),env('MISTRAL_MODEL','mistral-small-latest'),env_int('MISTRAL_MAX_TOKENS',1800),env_float('MISTRAL_TEMPERATURE',.35),
        env_int('LLM_TIMEOUT',90),env_float('LLM_MIN_INTERVAL_SECONDS',2),env_float('GROQ_COOLDOWN_SECONDS',30),
        env('DB_FILE','runtime/news.db'),env('SOURCE_CONFIG','sources.json'),env('AUTO_PUBLISH','0')=='1',max(60,env_int('POLL_INTERVAL',300)),max(1,env_int('DEDUP_HOURS',24)),env_float('SIMILARITY_THRESHOLD',.86),
        max(1.0, env_float('EVENT_RESOLVER_HOURS', 24)),max(1, env_int('EVENT_RESOLVER_MAX_CANDIDATES', 40)),max(0.0, env_float('EVENT_RESOLVER_SIMILARITY_FLOOR', .10)),max(0.0, min(1.0, env_float('EVENT_RESOLVER_CONFIDENCE', .78))),max(0.5, env_float('EVENT_RESOLVER_MIN_INTERVAL_SECONDS', 4.0)),max(40, env_int('EVENT_RESOLVER_SCAN_LIMIT', 200)),max(.80, min(1.0, env_float('EVENT_RESOLVER_FALLBACK_SIMILARITY', .94))),
        env_int('MAX_POST_LENGTH',800),env_int('RETENTION_DAYS',7),env('LOG_LEVEL','INFO'),env_int('MIN_PRIORITY_PUBLISH',0),env_float('MIN_CONFIDENCE_PUBLISH',.82),
        env('ADMIN_BOT_TOKEN'),tuple(ids),max(1,env_int('ADMIN_BOT_POLL_SECONDS',5)))
def load_sources(path):
    p=Path(path)
    if not p.exists(): return []
    data=json.loads(p.read_text(encoding='utf-8')); out=[]
    for s in data.get('sources',[]):
        u=str(s.get('username','')).lstrip('@').lower()
        if u: out.append({'username':u,'enabled':bool(s.get('enabled',True)),'priority':int(s.get('priority',5)),'category':str(s.get('category','auto')),'reliability':float(s.get('reliability',.7)),'owner':'primary'})
    return out
