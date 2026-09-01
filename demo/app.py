"""
app.py — веб-демо для Имплант-Дент
Запуск:  uvicorn app:app --reload --port 8000
Открой: http://localhost:8000
"""
import sys
try: sys.stdout.reconfigure(encoding='utf-8')
except: pass
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
import time

from bot import call_llm, build_messages, log_lead
from config import SYSTEM_PROMPT

app = FastAPI(title="Имплант-Дент Demo")

# статика
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

class ChatReq(BaseModel):
    messages: list  # [{role, content}]
    meta: dict | None = None

@app.get("/", response_class=HTMLResponse)
def index():
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)

@app.get("/api/prompt")
def get_prompt():
    # отдаём для отладки первые 3000 символов
    return {"prompt_preview": SYSTEM_PROMPT[:3000] + "...", "len": len(SYSTEM_PROMPT)}

@app.post("/api/chat")
def chat(req: ChatReq):
    # защита от пустых
    msgs = [m for m in req.messages if m.get("content")]
    if not msgs:
        return JSONResponse({"error": "empty"}, status_code=400)
    # ограничим историю 16 последних
    history = msgs[-16:]
    try:
        reply, model = call_llm(build_messages(history))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    # логируем лид если нашли
    try:
        log_lead(history + [{"role":"assistant","content":reply}], meta=req.meta)
    except: pass
    return {"reply": reply, "model": model, "ts": int(time.time())}

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}
