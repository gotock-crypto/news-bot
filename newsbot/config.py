import json, os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

def env(name, default=''):
    return os.getenv(name, default).strip()

def env_int(name, default):
    try: return int(env(name, str(default)))
    except ValueError: return default

def env_float(name, default):
    try: return float(env(name, str(default)))
    except ValueError: return default

@dataclass(frozen=True)
class Settings:
    tg_api_id:int; tg_api_hash:str; tg_phone:str; tg_session:str
    max_phone:str; max_session:str; max_work_dir:str; max_channel_id:str
    groq_key:str; groq_model:str; groq_max_tokens:int; groq_temperature:float
    mistral_key:str; mistral_model:str; mistral_max_tokens:int; mistral_temperature:float
    llm_timeout:int; llm_min_interval_seconds:float; groq_cooldown_seconds:float; db_file:str; source_config:str; auto_publish:bool
    backfill_limit:int; poll_interval:int; dedup_hours:int; similarity_threshold:float
    min_sources_high_conf:int; max_post_length:int; retention_days:int; log_level:str
    min_priority_publish:int; min_confidence_publish:float
    @property
    def db_path(self): return ROOT / self.db_file
    @property
    def source_path(self): return ROOT / self.source_config
    @property
    def max_dir(self): return ROOT / self.max_work_dir
    @property
    def tg_session_path(self): return str(ROOT / self.tg_session)

def load_settings():
    return Settings(
        tg_api_id=env_int('TG_API_ID',0), tg_api_hash=env('TG_API_HASH'), tg_phone=env('TG_PHONE'),
        tg_session=env('TG_SESSION','runtime/telegram/newsbot'),
        max_phone=env('MAX_PHONE'), max_session=env('MAX_SESSION','max.db'), max_work_dir=env('MAX_WORK_DIR','runtime/max'), max_channel_id=env('MAX_CHANNEL_ID'),
        groq_key=env('GROQ_API_KEY'), groq_model=env('GROQ_MODEL','qwen/qwen3-27b'), groq_max_tokens=env_int('GROQ_MAX_TOKENS',1800), groq_temperature=env_float('GROQ_TEMPERATURE',.35),
        mistral_key=env('MISTRAL_API_KEY'), mistral_model=env('MISTRAL_MODEL','mistral-small-latest'), mistral_max_tokens=env_int('MISTRAL_MAX_TOKENS',1800), mistral_temperature=env_float('MISTRAL_TEMPERATURE',.35),
        llm_timeout=env_int('LLM_TIMEOUT',90), llm_min_interval_seconds=env_float('LLM_MIN_INTERVAL_SECONDS',2.0), groq_cooldown_seconds=env_float('GROQ_COOLDOWN_SECONDS',30.0), db_file=env('DB_FILE','runtime/news.db'), source_config=env('SOURCE_CONFIG','sources.json'),
        auto_publish=env('AUTO_PUBLISH','0')=='1', backfill_limit=env_int('BACKFILL_LIMIT',100), poll_interval=env_int('POLL_INTERVAL',5), dedup_hours=env_int('DEDUP_HOURS',36), similarity_threshold=env_float('SIMILARITY_THRESHOLD',.86),
        min_sources_high_conf=env_int('MIN_SOURCES_FOR_HIGH_CONFIDENCE',2), max_post_length=env_int('MAX_POST_LENGTH',3500), retention_days=env_int('RETENTION_DAYS',4), log_level=env('LOG_LEVEL','INFO'),
        min_priority_publish=env_int('MIN_PRIORITY_PUBLISH',70), min_confidence_publish=env_float('MIN_CONFIDENCE_PUBLISH',.82)
    )

def load_sources(path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    result=[]
    for s in data.get('sources',[]):
        if not s.get('enabled'): continue
        x=dict(s)
        x['username']=str(x['username']).lstrip('@')
        x['priority']=int(x.get('priority',5))
        x['reliability']=float(x.get('reliability',0.7))
        x['category']=x.get('category','auto')
        result.append(x)
    return result
