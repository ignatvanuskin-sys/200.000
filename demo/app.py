"""
app.py — веб-демо для Имплант-Дент
Запуск:  uvicorn app:app --reload --port 8000
Открой: http://localhost:8000
"""
import sys
try: sys.stdout.reconfigure(encoding='utf-8')
except: pass
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from pathlib import Path
import time
from collections import defaultdict, deque

from bot import call_llm, build_messages, log_lead
from config import SYSTEM_PROMPT

app = FastAPI(title="Имплант-Дент Demo")

# CORS — ограничим для демо, в проде поставь конкретный домен
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["Content-Type"],
)

# статика
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Rate limit: 20 req / 60s per IP (in-memory, для прод — Redis, см. README)
_rate = defaultdict(deque)
def is_rate_limited(ip: str, limit=20, window=60) -> bool:
    now = time.time()
    q = _rate[ip]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False

@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    # 100KB hard limit — защита от abuse
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > 100*1024:
        return JSONResponse({"error":"payload too large"}, status_code=413)
    return await call_next(request)

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1, max_length=2000)

class ChatReq(BaseModel):
    messages: list[ChatMessage]
    meta: dict | None = None
    @validator('messages')
    def check_len(cls, v):
        if len(v) > 16:
            raise ValueError('too many messages (max 16)')
        return v

@app.get("/", response_class=HTMLResponse)
def index():
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)

@app.get("/api/prompt")
def get_prompt():
    # отдаём для отладки первые 3000 символов
    return {"prompt_preview": SYSTEM_PROMPT[:3000] + "...", "len": len(SYSTEM_PROMPT)}

@app.post("/api/chat")
def chat(req: ChatReq, request: Request):
    ip = request.client.host if request.client else "unknown"
    if is_rate_limited(ip):
        return JSONResponse({"error": "rate_limited, try again in 60s"}, status_code=429)
    # Pydantic уже валидировал, конвертим в dict
    history = [{"role": m.role, "content": m.content.strip()[:2000]} for m in req.messages if m.content.strip()]
    if not history:
        return JSONResponse({"error": "empty"}, status_code=400)
    try:
        reply, model = call_llm(build_messages(history))
    except Exception as e:
        # не утекаем внутрь
        print(f"[chat error] {e}")
        return JSONResponse({"error": "LLM temporarily unavailable, try again"}, status_code=502)
    # логируем лид если нашли
    try:
        log_lead(history + [{"role":"assistant","content":reply}], meta=req.meta)
    except: pass
    return {"reply": reply, "model": model, "ts": int(time.time())}

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}
