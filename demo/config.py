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

# Загружаем системный промпт из файла выше
PROMPT_PATH = Path(__file__).parent.parent / "PROMPT_Имплант-Дент.md"
if PROMPT_PATH.exists():
    SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")
else:
    SYSTEM_PROMPT = "Ты — администратор стоматологии Имплант-Дент."

# Лид-лог (куда писать заявки демо)
LEADS_FILE = Path(__file__).parent / "leads.jsonl"
