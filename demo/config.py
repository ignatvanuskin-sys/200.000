import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# --- Gemini (основной, бесплатный, хватает на весь день) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# gemini-3.6-flash = 1500 RPD / 15 RPM в free tier, лучший на сегодня. Фолбэки — lighter модели.
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]

# --- OpenRouter (фолбэк, если Gemini по квоте) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODELS = [m.strip() for m in os.getenv("OPENROUTER_MODELS", "minimax/minimax-m3:free,liquid/lfm-2.5-2.6b:free").split(",") if m.strip()]
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "http://localhost:8000")
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "Implant-Dent Demo")

# --- WhatsApp Cloud API (Meta) ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")  # для проверки подписи X-Hub-Signature-256

# --- Telegram (уведомления админу о лидах) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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

# Лид-лог (куда писать заявки демо)
LEADS_FILE = Path(__file__).parent / "leads.jsonl"
