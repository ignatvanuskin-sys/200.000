"""
app.py — FastAPI приложение для Имплант-Дент на Railway
- GET  /webhook/whatsapp  — верификация webhook от Meta
- POST /webhook/whatsapp  — быстрый ack (<300ms) + background-обработка
- POST /api/chat           — чат для веб-демо (Имплант-Дент)
- POST /api/chat-dentica   — чат для Dentica (демо-клиника)
- GET  /health             — healthcheck (без зависимостей, быстрый всегда)
- GET  /                   — веб-демо (static/index.html)
Запуск:  uvicorn app:app --host 0.0.0.0 --port $PORT

NOTE про BackgroundTasks: это НЕ durable queue — задача теряется при
SIGTERM/restart между ack и завершением. Dedupe-claim делается ДО ack,
поэтому Meta-ретрай после падения вернёт "duplicate" (честное ограничение
задокументировано; для durable нужен Redis Streams / RQ — см. store.py).
"""
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

logging.basicConfig(level="INFO",
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("implant-dent.app")

from fastapi import FastAPI, Request, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
try:
    from pydantic import field_validator as _fv
except ImportError:  # pydantic v1 fallback
    from pydantic import validator as _v

    def _fv(*a, **k):
        return _v(*a, **k)

import config
from config import SYSTEM_PROMPT, DENTICA_PROMPT, MAX_BODY_BYTES
import bot
from bot import mask_phone, scrub
from store import ProdStore

store = ProdStore(
    dedupe_ttl_s=config.WA_DEDUPE_TTL_S,
    rate_limit=config.WA_RATE_LIMIT,
    rate_window_s=config.WA_RATE_WINDOW_S,
    history_max=config.WA_HISTORY_MAX,
    history_ttl_s=config.WA_HISTORY_TTL_S,
    redis_url=config.REDIS_URL,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger().setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    # httpx пишет полный URL (включая ?key=...) в access-лог — глушим, иначе ключ в Railway-логах
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config.validate_on_startup()
        log.info("config ok env=%s redis=%s models=%d",
                 config.APP_ENV, bool(config.REDIS_URL), len(config.GEMINI_MODELS))
    except Exception as e:
        log.error("startup validation failed: %s", e)
        raise
    # прогрев shared httpx-клиента (пул создаётся лениво при первом запросе)
    try:
        bot.get_async_client()
    except Exception as e:
        log.warning("http client init failed (will retry lazily): %s", str(e)[:150])
    yield
    try:
        await bot.close_http_clients()
    except Exception:
        pass
    log.info("shutdown complete")


app = FastAPI(title="Имплант-Дент Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.middleware("http")
async def request_id_mw(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.rid = rid
    t0 = time.time()
    # защита от chunked-обхода Content-Length: режем явно большие заголовки
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    resp = await call_next(request)
    dt = (time.time() - t0) * 1000
    resp.headers["X-Request-ID"] = rid
    log.info("rid=%s %s %s -> %d %.0fms", rid, request.method, request.url.path,
             resp.status_code, dt)
    return resp


# ===================== WhatsApp Webhook =====================

@app.get("/webhook/whatsapp")
def whatsapp_verify(
    request: Request,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """GET-верификация webhook от Meta Cloud API. Fail-closed при пустом токене."""
    expected = config.WHATSAPP_VERIFY_TOKEN
    if not expected:
        log.warning("rid=%s wa verify refused: no verify token configured", request.state.rid)
        return PlainTextResponse("verify not configured", status_code=403)
    if hub_mode == "subscribe" and hub_verify_token == expected:
        return PlainTextResponse(hub_challenge or "")
    return PlainTextResponse("verify failed", status_code=403)


async def _bg_text(from_id: str, text: str, contact_name: str, rid: str):
    try:
        await bot.run_claimed_text(store, from_id, text, contact_name, rid)
    except Exception as e:
        log.warning("rid=%s background text failed: %s", rid, str(e)[:200])


async def _bg_static(to: str, rid: str):
    try:
        await bot.run_static_reply(to, rid)
    except Exception as e:
        log.warning("rid=%s background static failed: %s", rid, str(e)[:200])


@app.post("/webhook/whatsapp")
async def whatsapp_receive(request: Request, background: BackgroundTasks):
    """Быстрый ack: validate/parse/claim/rate — и сразу 200. LLM — в background."""
    rid = request.state.rid
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    signature = request.headers.get("x-hub-signature-256", "") or \
        request.headers.get("X-Hub-Signature-256", "")
    try:
        import json
        body = json.loads(raw_body) if raw_body else {}
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    decision, data = bot.precheck_webhook(store, body, raw_body, signature)
    if decision == "ack_only":
        return JSONResponse({"status": data.get("status")}, status_code=data.get("http_code", 200))
    if decision == "send_static":
        background.add_task(_bg_static, data["from_id"], rid)
        return JSONResponse({"status": "queued-static"}, status_code=200)
    background.add_task(_bg_text, data["from_id"], data["text"],
                        data.get("contact_name", "Unknown"), rid)
    return JSONResponse({"status": "queued"}, status_code=200)


# ===================== Чат для веб-демо =====================

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1, max_length=2000)


class ChatReq(BaseModel):
    messages: list[ChatMessage]
    meta: dict | None = None

    @_fv('messages')
    @classmethod
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


async def _chat_core(history: list, request: Request, scope: str):
    ip = request.client.host if request.client else "unknown"
    if store.is_rate_limited(scope, ip, config.API_CHAT_RATE_LIMIT, config.API_CHAT_RATE_WINDOW_S):
        return JSONResponse({"error": "rate_limited, try again in 60s"}, status_code=429)
    if not history:
        return JSONResponse({"error": "empty"}, status_code=400)
    rid = request.state.rid
    try:
        reply, model = await bot.call_llm_async(history, request_id=rid)
    except Exception as e:
        log.warning("rid=%s chat llm failed: %s", rid, scrub(str(e))[:200])
        return JSONResponse({"error": "LLM temporarily unavailable, try again"}, status_code=502)
    return {"reply": reply, "model": model, "ts": int(time.time())}


@app.post("/api/chat")
async def chat(req: ChatReq, request: Request):
    history = [{"role": m.role, "content": m.content.strip()[:2000]}
               for m in req.messages if m.content.strip()]
    if not history:
        return JSONResponse({"error": "empty"}, status_code=400)
    res = await _chat_core(bot.build_messages(history), request, "api-chat")
    if isinstance(res, dict):
        try:
            bot.log_lead(history + [{"role": "assistant", "content": res["reply"]}])
        except Exception:
            pass
    return res


@app.post("/api/chat-dentica")
async def chat_dentica(req: ChatReq, request: Request):
    """Чат-эндпоинт для Dentica — отдельный промпт, отдельный rate-bucket."""
    clean = []
    for m in req.messages[-16:]:
        role = m.role if m.role in ("user", "assistant", "system") else "user"
        content = (m.content or "").strip()[:2000]
        if content:
            clean.append({"role": role, "content": content})
    # системный промпт — только Dentica, пользовательский system запрещён
    clean = [m for m in clean if m["role"] != "system"]
    if not clean:
        return JSONResponse({"error": "empty"}, status_code=400)
    msgs = [{"role": "system", "content": DENTICA_PROMPT}] + clean
    return await _chat_core(msgs, request, "api-dentica")


# ===================== Health =====================

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time()), "store": store.stats()}
