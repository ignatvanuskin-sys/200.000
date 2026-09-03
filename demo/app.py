"""
app.py — FastAPI приложение для Имплант-Дент на Railway
- GET  /webhook/whatsapp  — верификация webhook от Meta
- POST /webhook/whatsapp  — приём входящих WhatsApp-сообщений
- POST /api/chat           — чат для веб-демо (Имплант-Дент)
- POST /api/chat-dentica   — чат для Dentica (демо-клиника)
- GET  /health             — healthcheck
- GET  /                   — веб-демо (static/index.html)
Запуск:  uvicorn app:app --host 0.0.0.0 --port $PORT
"""
import sys
try: sys.stdout.reconfigure(encoding='utf-8')
except: pass
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from pathlib import Path
import time, re
from collections import defaultdict, deque

from bot import call_llm, build_messages, log_lead, process_whatsapp_message
from config import SYSTEM_PROMPT, DENTICA_PROMPT, WHATSAPP_VERIFY_TOKEN

app = FastAPI(title="Имплант-Дент Bot")

# CORS
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

# Rate limit: 20 req / 60s per IP (in-memory, для прод — Redis)
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


# ===================== WhatsApp Webhook =====================

@app.get("/webhook/whatsapp")
def whatsapp_verify(
    request: Request,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """GET-эндпоинт для верификации webhook от Meta Cloud API."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        print(f"[wa] verify OK, challenge={hub_challenge}")
        return PlainTextResponse(hub_challenge or "")
    print(f"[wa] verify FAILED, got token=...{hub_verify_token[-4:] if hub_verify_token else 'none'}")
    return PlainTextResponse("verify failed", status_code=403)

@app.post("/webhook/whatsapp")
async def whatsapp_receive(request: Request):
    """POST-эндпоинт для приёма входящих WhatsApp-сообщений."""
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "") or request.headers.get("X-Hub-Signature-256", "")
    try:
        import json
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    status_code, result = process_whatsapp_message(body, raw_body, signature)
    return JSONResponse(result, status_code=status_code)


# ===================== Чат для веб-демо =====================

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
    return {"prompt_preview": SYSTEM_PROMPT[:3000] + "...", "len": len(SYSTEM_PROMPT)}

@app.post("/api/chat")
def chat(req: ChatReq, request: Request):
    ip = request.client.host if request.client else "unknown"
    if is_rate_limited(ip):
        return JSONResponse({"error": "rate_limited, try again in 60s"}, status_code=429)
    history = [{"role": m.role, "content": m.content.strip()[:2000]} for m in req.messages if m.content.strip()]
    if not history:
        return JSONResponse({"error": "empty"}, status_code=400)
    try:
        reply, model = call_llm(build_messages(history))
    except Exception as e:
        print(f"[chat error] {e}")
        return JSONResponse({"error": "LLM temporarily unavailable, try again"}, status_code=502)
    try:
        log_lead(history + [{"role":"assistant","content":reply}], meta=req.meta)
    except: pass
    return {"reply": reply, "model": model, "ts": int(time.time())}


# ===================== Dentica (демо-клиника) =====================

@app.post("/api/chat-dentica")
def chat_dentica(req: ChatReq, request: Request):
    """Чат-эндпоинт для Dentica — отдельный промпт, та же логика."""
    ip = request.client.host if request.client else "unknown"
    if is_rate_limited(ip):
        return JSONResponse({"error": "rate_limited, try again in 60s"}, status_code=429)
    history = [{"role": m.role, "content": m.content.strip()[:2000]} for m in req.messages if m.content.strip()]
    if not history:
        return JSONResponse({"error": "empty"}, status_code=400)
    # Бизнес-логика та же, только системный промпт — Dentica
    clean = []
    for m in history[-16:]:
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = (m.get("content", "") or "").strip()[:2000]
        if content:
            clean.append({"role": role, "content": content})
    msgs = [{"role": "system", "content": DENTICA_PROMPT}] + clean
    try:
        reply, model = call_llm(msgs)
    except Exception as e:
        print(f"[dentica error] {e}")
        return JSONResponse({"error": "LLM temporarily unavailable, try again"}, status_code=502)
    return {"reply": reply, "model": model, "ts": int(time.time())}


# ===================== Health =====================

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}
