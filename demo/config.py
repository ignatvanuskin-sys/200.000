"""config.py — конфигурация Имплант-Дент.

Канонические имена переменных (старые короткие алиасы поддерживаются как fallback):
  WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
  WHATSAPP_APP_SECRET, GEMINI_API_KEY, GEMINI_MODELS,
  OPENROUTER_API_KEY, OPENROUTER_MODELS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  REDIS_URL, APP_ENV, ALLOWED_ORIGINS, LOG_LEVEL
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

APP_ENV = os.getenv("APP_ENV", "").lower() or (
    "production" if os.getenv("RAILWAY_ENVIRONMENT_NAME", "").lower() == "production" else "development"
)
IS_PROD = APP_ENV == "production"
IS_TEST = APP_ENV == "test"


def _get(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n, "")
        if v:
            return v
    return os.getenv(names[0], default) if names else default


# --- Gemini (primary) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]

# --- OpenRouter (fallback, только если задан ключ) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODELS = [m.strip() for m in os.getenv(
    "OPENROUTER_MODELS", "minimax/minimax-m3:free,liquid/lfm-2.5-2.6b:free").split(",") if m.strip()]
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "http://localhost:8000")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "Implant-Dent Demo")

# --- WhatsApp Cloud API (Meta), канонические имена + короткие алиасы ---
WHATSAPP_TOKEN = _get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = _get("WHATSAPP_PHONE_NUMBER_ID", "PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = _get("WHATSAPP_VERIFY_TOKEN", "VERIFY_TOKEN")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

# --- Telegram (уведомления о лидах) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Persistent state (опционально; без него — in-memory fallback на 1 реплику) ---
REDIS_URL = os.getenv("REDIS_URL", "")

# --- HTTP ---
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "5"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "12"))
HTTP_POOL_MAX = int(os.getenv("HTTP_POOL_MAX", "20"))

# --- Rate limits ---
WA_RATE_LIMIT = int(os.getenv("WA_RATE_LIMIT", "10"))
WA_RATE_WINDOW_S = int(os.getenv("WA_RATE_WINDOW_S", "60"))
API_CHAT_RATE_LIMIT = int(os.getenv("API_CHAT_RATE_LIMIT", "20"))
API_CHAT_RATE_WINDOW_S = int(os.getenv("API_CHAT_RATE_WINDOW_S", "60"))

# --- Dialog ---
WA_HISTORY_MAX = int(os.getenv("WA_HISTORY_MAX", "10"))
WA_HISTORY_TTL_S = int(os.getenv("WA_HISTORY_TTL_S", "86400"))  # 24ч
WA_DEDUPE_TTL_S = int(os.getenv("WA_DEDUPE_TTL_S", "300"))  # 5 мин
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(100 * 1024)))

# --- Misc ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# --- Dentica (демо-клиника, отдельный промпт) ---
DENTICA_PROMPT = os.getenv("DENTICA_PROMPT", """# Промпт: ИИ-администратор Dentica (Алматы)

Ты — администратор стоматологии Dentica в городе Алматы. Адрес: пр. Абая, 115, 2 этаж. Телефон +7 701 123 45 67, WhatsApp +7 701 123 45 67. Приём по записи. Цены: имплант от 140 000 ₸, чистка УЗ 15 000 ₸, кариес 12 000–26 000 ₸. Календарь не подключён — не называй окна, договаривайся о звонке. Отвечай коротко, 1-2 строки, сначала ответь, потом один вопрос. Не выдумывай врачей/акций.
""")

# Загружаем системный промпт — ищем в demo/ и в корне (для Railway Root=demo)
for _cand in [
    Path(__file__).parent.parent / "PROMPT_Имплант-Дент.md",
    Path(__file__).parent / "PROMPT_Имплант-Дент.md",
    Path.cwd() / "PROMPT_Имплант-Дент.md",
    Path.cwd() / "demo" / "PROMPT_Имплант-Дент.md",
]:
    if _cand.exists():
        SYSTEM_PROMPT = _cand.read_text(encoding="utf-8")
        break
else:
    SYSTEM_PROMPT = "Ты — администратор стоматологии Имплант-Дент."

# Лид-лог (best-effort fallback; primary — Telegram + stdout; на Railway FS эфемерен)
LEADS_FILE = Path(__file__).parent / "leads.jsonl"


class ConfigError(RuntimeError):
    pass


def validate_on_startup() -> None:
    """Fail-fast в production, warn в dev/test. Возвращает None если всё ок."""
    import logging
    log = logging.getLogger("implant-dent.config")
    problems: list[str] = []
    if not (GEMINI_API_KEY or OPENROUTER_API_KEY):
        problems.append("GEMINI_API_KEY/OPENROUTER_API_KEY: нужен хотя бы один LLM-ключ")
    if not WHATSAPP_VERIFY_TOKEN:
        problems.append("WHATSAPP_VERIFY_TOKEN (или VERIFY_TOKEN): иначе Meta-верификация открыта")
    if not WHATSAPP_APP_SECRET:
        problems.append("WHATSAPP_APP_SECRET: иначе webhook-подпись не проверяется (fail-open)")
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        problems.append("WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID: иначе отправка ответов невозможна")
    if not problems:
        return
    msg = "config problems: " + "; ".join(problems)
    if IS_PROD:
        raise ConfigError(msg)
    log.warning("%s (APP_ENV=%s, старт разрешён вне production)", msg, APP_ENV)
