"""
bot.py — ядро бота Имплант-Дент.

Архитектура (post-fix):
  webhook fast path (parse + claim + rate, без I/O) → 200 сразу
  background       (LLM → WhatsApp → Telegram)      → durable насколько позволяют BackgroundTasks

Совместимость: call_llm / call_gemini / call_openrouter / build_messages /
extract_lead / log_lead / send_whatsapp / process_whatsapp_message сохранены
как sync-обёртки (тесты, CLI, старые вызывающие).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from pathlib import Path

import config
from config import (
    GEMINI_API_KEY, GEMINI_MODELS, OPENROUTER_API_KEY, OPENROUTER_MODELS,
    OPENROUTER_REFERER, OPENROUTER_TITLE, SYSTEM_PROMPT, LEADS_FILE,
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT, HTTP_POOL_MAX,
)

log = logging.getLogger("implant-dent.bot")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Статический ответ на неподдерживаемые типы (без LLM-вызова)
UNSUPPORTED_REPLY = (
    "Вижу, что вы отправили файл или голосовое. Опишите, пожалуйста, "
    "вопрос текстом — и я помогу. Если удобнее, позвоните: +7 708 543-63-18."
)


def mask_phone(phone: str) -> str:
    s = str(phone or "")
    return "..." + s[-4:] if len(s) > 4 else "..."


_KEY_RE = re.compile(r"(AIza[0-9A-Za-z\-_]{10,}|EAAG[0-9A-Za-z]+|sk-or-v1-[0-9A-Za-z\-]+)")


def scrub(s: str) -> str:
    """Вычистить возможные ключи из текста перед логом."""
    return _KEY_RE.sub("***", str(s))


# ===================== HTTP (async, pooled) =====================

_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def get_async_client():
    """Ленивый shared httpx.AsyncClient (connection pooling, один на процесс)."""
    import httpx
    with _clients_lock:
        c = _clients.get("main")
        if c is None:
            timeout = httpx.Timeout(connect=config.HTTP_CONNECT_TIMEOUT,
                                    read=config.HTTP_READ_TIMEOUT,
                                    write=10.0, pool=5.0)
            limits = httpx.Limits(max_connections=HTTP_POOL_MAX,
                                  max_keepalive_connections=10)
            c = httpx.AsyncClient(timeout=timeout, limits=limits)
            _clients["main"] = c
        return c


async def close_http_clients() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for c in clients:
        try:
            await c.aclose()
        except Exception:
            pass


def _run_sync(coro):
    """Sync-обёртка: работает и вне loop (asyncio.run), и внутри (отдельный поток)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict = {}
    def _runner():
        result["v"] = asyncio.run(coro)
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=200)
    if t.is_alive():
        raise RuntimeError("sync wrapper timeout")
    if "e" in result:
        raise result["e"]
    return result.get("v")


# ===================== LLM =====================

class LLMError(RuntimeError):
    def __init__(self, provider: str, kind: str, detail: str = ""):
        super().__init__(f"{provider}/{kind}: {detail[:200]}")
        self.provider = provider
        self.kind = kind  # auth|not_found|rate|server|timeout|network|empty|skipped


def _classify_exc(e: Exception) -> str:
    import httpx
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if isinstance(e, (httpx.ConnectError, httpx.NetworkError)):
        return "network"
    return "network"


def _gemini_payload(messages, max_tokens=600, temperature=0.6):
    system_text = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    history = messages[1:] if system_text else messages
    contents = []
    for m in history:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Привет"}]}]
    return {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }


async def call_gemini_async(messages, temperature=0.6, max_tokens=600, request_id="-"):
    if not GEMINI_API_KEY:
        raise LLMError("gemini", "skipped", "no key")
    import httpx
    client = get_async_client()
    last_err: Exception | None = None
    for model in GEMINI_MODELS:
        mname = model.replace("models/", "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mname}:generateContent"
        payload = _gemini_payload(messages, max_tokens=max_tokens, temperature=temperature)
        t0 = time.time()
        try:
            r = await client.post(url, params={"key": GEMINI_API_KEY}, json=payload)
            dt = time.time() - t0
            if r.status_code == 200:
                data = r.json()
                cand = (data.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts).strip()
                if not text:
                    last_err = LLMError("gemini", "empty", f"{model} empty")
                    log.info("rid=%s llm=gemini model=%s status=empty dt=%.2f", request_id, model, dt)
                    continue
                log.info("rid=%s llm=gemini model=%s status=ok dt=%.2f len=%d",
                         request_id, model, dt, len(text))
                return text, model
            if r.status_code in (401, 403):
                # fail fast: ключ/доступ — ретраи бессмысленны
                raise LLMError("gemini", "auth", f"{model} -> {r.status_code}")
            if r.status_code == 404:
                # fail fast: модели нет — не ждём, идём дальше
                last_err = LLMError("gemini", "not_found", f"{model} -> 404")
                log.info("rid=%s llm=gemini model=%s status=404 dt=%.2f", request_id, model, dt)
                continue
            if r.status_code == 429:
                last_err = LLMError("gemini", "rate", f"{model} -> 429")
                log.info("rid=%s llm=gemini model=%s status=429 dt=%.2f", request_id, model, dt)
                await asyncio.sleep(1.0)  # короткий controlled backoff, не 30с
                continue
            last_err = LLMError("gemini", "server", f"{model} -> {r.status_code}")
            log.info("rid=%s llm=gemini model=%s status=%d dt=%.2f", request_id, model, r.status_code, dt)
        except LLMError:
            raise
        except Exception as e:
            kind = _classify_exc(e)
            last_err = LLMError("gemini", kind, f"{model} {e}")
            log.info("rid=%s llm=gemini model=%s status=%s dt=%.2f", request_id, model, kind, time.time() - t0)
    raise last_err or LLMError("gemini", "server", "no models")


async def call_openrouter_async(messages, temperature=0.6, max_tokens=600, request_id="-"):
    if not OPENROUTER_API_KEY:
        raise LLMError("openrouter", "skipped", "no key")
    client = get_async_client()
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }
    last_err: Exception | None = None
    for model in OPENROUTER_MODELS:
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        t0 = time.time()
        try:
            r = await client.post(OPENROUTER_URL, headers=headers, json=payload)
            dt = time.time() - t0
            if r.status_code == 200:
                data = r.json()
                content = (((data.get("choices") or [{}])[0].get("message")) or {}).get("content")
                if not content:
                    last_err = LLMError("openrouter", "empty", f"{model} empty")
                    continue
                log.info("rid=%s llm=openrouter model=%s status=ok dt=%.2f", request_id, model, dt)
                return str(content).strip(), model
            if r.status_code in (401, 403):
                raise LLMError("openrouter", "auth", f"{model} -> {r.status_code}")
            if r.status_code == 404:
                last_err = LLMError("openrouter", "not_found", f"{model} -> 404")
                log.info("rid=%s llm=openrouter model=%s status=404 dt=%.2f", request_id, model, dt)
                continue
            if r.status_code == 429:
                last_err = LLMError("openrouter", "rate", f"{model} -> 429")
                log.info("rid=%s llm=openrouter model=%s status=429 dt=%.2f", request_id, model, dt)
                await asyncio.sleep(1.0)
                continue
            last_err = LLMError("openrouter", "server", f"{model} -> {r.status_code}")
            log.info("rid=%s llm=openrouter model=%s status=%d dt=%.2f", request_id, model, r.status_code, dt)
        except LLMError:
            raise
        except Exception as e:
            kind = _classify_exc(e)
            last_err = LLMError("openrouter", kind, f"{model} {e}")
            log.info("rid=%s llm=openrouter model=%s status=%s dt=%.2f", request_id, model, kind, time.time() - t0)
    raise last_err or LLMError("openrouter", "server", "no models")


async def call_llm_async(messages, temperature=0.6, max_tokens=600, request_id="-"):
    """Gemini primary, OpenRouter fallback (только если есть ключ)."""
    try:
        return await call_gemini_async(messages, temperature, max_tokens, request_id)
    except LLMError as e_gem:
        if e_gem.kind == "auth":
            log.warning("rid=%s gemini auth failure: %s", request_id, str(e_gem)[:150])
        if OPENROUTER_API_KEY:
            try:
                return await call_openrouter_async(messages, temperature, max_tokens, request_id)
            except Exception as e_or:
                raise RuntimeError(f"Gemini fail: {e_gem} | OpenRouter fail: {e_or}")
        raise


# sync-обёртки (тесты, CLI, старые вызывающие)
def call_gemini(messages, temperature=0.6, max_tokens=600):
    return _run_sync(call_gemini_async(messages, temperature, max_tokens))


def call_openrouter(messages, temperature=0.6, max_tokens=600):
    return _run_sync(call_openrouter_async(messages, temperature, max_tokens))


def call_llm(messages, temperature=0.6, max_tokens=600):
    return _run_sync(call_llm_async(messages, temperature, max_tokens))


call_openrouter_compat = call_llm  # alias для совместимости


def _sanitize(text: str, limit=2000) -> str:
    return (text or "").strip()[:limit]


def build_messages(history):
    clean = []
    for m in history[-16:]:
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = _sanitize(m.get("content", ""))
        if content:
            clean.append({"role": role, "content": content})
    return [{"role": "system", "content": SYSTEM_PROMPT}] + clean


def extract_lead(history):
    text = " ".join(m["content"] for m in history if m.get("role") == "user")
    phone = None
    m = re.search(r"(\+7\s?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})", text)
    if not m:
        m = re.search(r"(8\s?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})", text)
    if m:
        phone = m.group(1)
    name = None
    m2 = re.search(r"(?:меня зовут|я)\s+([А-ЯЁа-яё]+)", text, re.I)
    if m2:
        name = m2.group(1)
    return name, phone


_lead_lock = threading.Lock()


def log_lead(history, meta=None):
    """Primary — structured stdout (Railway logs); файл — best-effort fallback."""
    name, phone = extract_lead(history)
    if not (name or phone):
        return None
    rec = {"ts": int(time.time()), "name": name,
           "phone_masked": mask_phone(phone) if phone else None,
           "history_len": len(history), "meta": meta}
    log.info("lead captured name=%s phone=%s history_len=%d", name, rec["phone_masked"], len(history))
    try:
        if LEADS_FILE.exists() and LEADS_FILE.stat().st_size > 1_000_000:
            backup = LEADS_FILE.with_suffix(".1.jsonl")
            try:
                if backup.exists():
                    backup.unlink()
                LEADS_FILE.rename(backup)
            except Exception:
                pass
        full = {"ts": rec["ts"], "name": name, "phone": phone,
                "history": history[-6:], "meta": meta}
        with _lead_lock:
            with open(LEADS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(full, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("lead file log failed: %s", scrub(str(e))[:150])
    return rec


# ===================== WhatsApp =====================

def verify_wa_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def signature_enforced() -> bool:
    """Fail-closed в production: без секрета webhook не принимаем."""
    if WHATSAPP_APP_SECRET:
        return True
    return not config.IS_PROD


def parse_whatsapp_payload(body: dict) -> dict:
    """Чистый парсинг Meta payload. Без I/O — безопасно в fast path."""
    try:
        entry = (body.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {}) or {}
    except Exception:
        return {"kind": "non-message"}
    msgs = value.get("messages") or []
    if not msgs:
        return {"kind": "non-message"}
    msg = msgs[0] or {}
    msg_id = msg.get("id", "")
    from_id = msg.get("from", "")
    contacts = value.get("contacts") or [{}]
    contact_name = ((contacts[0] or {}).get("profile") or {}).get("name") or "Unknown"
    if msg.get("type") != "text":
        return {"kind": "non-text", "msg_id": msg_id, "from_id": from_id,
                "contact_name": contact_name, "msg_type": msg.get("type")}
    text = ((msg.get("text") or {}).get("body") or "").strip()[:2000]
    if not text:
        return {"kind": "empty", "msg_id": msg_id, "from_id": from_id}
    if not re.match(r"^\d{7,15}$", from_id or ""):
        return {"kind": "invalid-phone", "msg_id": msg_id, "from_id": from_id}
    return {"kind": "text", "msg_id": msg_id, "from_id": from_id,
            "text": text, "contact_name": contact_name}


async def send_whatsapp_async(to: str, text: str, request_id="-") -> dict:
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID not set")
    if not re.match(r"^\d{7,15}$", to or ""):
        raise ValueError(f"invalid phone: {mask_phone(to)}")
    client = get_async_client()
    t0 = time.time()
    r = await client.post(
        f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                 "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": "text",
              "text": {"body": text[:4000]}},
    )
    dt = time.time() - t0
    if r.status_code >= 400:
        log.warning("rid=%s whatsapp send status=%d dt=%.2f to=%s err=%s",
                    request_id, r.status_code, dt, mask_phone(to), r.text[:200])
        raise RuntimeError(f"wa send {r.status_code} {r.text[:200]}")
    log.info("rid=%s whatsapp send ok dt=%.2f to=%s", request_id, dt, mask_phone(to))
    return r.json()


def send_whatsapp(to: str, text: str):
    return _run_sync(send_whatsapp_async(to, text))


async def notify_admin_telegram_async(text: str, from_id: str, request_id="-",
                                      retries: int = 1) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("rid=%s notify skip (no telegram config) from=%s", request_id, mask_phone(from_id))
        return False
    client = get_async_client()
    last_e = None
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID,
                      "text": f"📥 Заявка {mask_phone(from_id)}: {text[:500]}"},
            )
            if r.status_code == 200:
                log.info("rid=%s telegram notify ok dt=%.2f", request_id, time.time() - t0)
                return True
            last_e = f"status {r.status_code}"
        except Exception as e:
            last_e = scrub(str(e))[:150]
        if attempt < retries:
            await asyncio.sleep(1.0)
    log.warning("rid=%s telegram notify failed: %s", request_id, last_e)
    return False


def _is_lead(user_text: str, reply: str) -> bool:
    has_phone = bool(re.search(r"\+7|8\s*\(?\d{3}", user_text or "") or
                     re.search(r"\+7|8\s*\(?\d{3}", reply or ""))
    return has_phone or ("Передал администратору" in (reply or ""))


# ---------- fast path: вызывается в webhook до 200 (только CPU/RAM, без I/O) ----------
def precheck_webhook(store, body: dict, raw_body: bytes, signature: str):
    """-> (decision, data). decision: process_text | send_static | ack_only."""
    if not signature_enforced():
        return "ack_only", {"http_code": 503, "status": "signature not configured"}
    if WHATSAPP_APP_SECRET and not verify_wa_signature(raw_body, signature, WHATSAPP_APP_SECRET):
        return "ack_only", {"http_code": 401, "status": "invalid signature"}
    parsed = parse_whatsapp_payload(body)
    kind = parsed["kind"]
    if kind in ("non-message", "empty"):
        return "ack_only", {"http_code": 200, "status": f"ignored {kind}"}
    if kind == "invalid-phone":
        return "ack_only", {"http_code": 200, "status": "invalid phone"}
    msg_id = parsed.get("msg_id", "")
    if msg_id and not store.claim_seen(msg_id):
        return "ack_only", {"http_code": 200, "status": "duplicate"}
    from_id = parsed.get("from_id", "")
    if store.is_rate_limited("wa", from_id, config.WA_RATE_LIMIT, config.WA_RATE_WINDOW_S):
        return "ack_only", {"http_code": 200, "status": "rate limited"}
    if kind == "non-text":
        return "send_static", parsed
    return "process_text", parsed


# ---------- background: LLM → WhatsApp → Telegram ----------
async def run_claimed_text(store, from_id: str, text: str, contact_name: str,
                           request_id="-") -> dict:
    store.push_history(from_id, "user", text)
    hist = store.get_history(from_id)
    all_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + hist
    t0 = time.time()
    try:
        reply, model = await call_llm_async(all_messages, request_id=request_id)
    except Exception as e:
        log.warning("rid=%s llm failed dt=%.2f from=%s err=%s",
                    request_id, time.time() - t0, mask_phone(from_id), scrub(str(e))[:200])
        # Юзер уже ждёт: честный fallback (без зацикливания), HTTP-уровень при этом 200
        try:
            await send_whatsapp_async(from_id, "Сбой, попробуйте ещё раз. Оператор свяжется.",
                                      request_id)
            return {"status": "llm-failed-fallback-sent", "error": scrub(str(e))[:200]}
        except Exception as e2:
            log.warning("rid=%s fallback send failed: %s", request_id, str(e2)[:150])
            return {"status": "llm-failed", "error": scrub(str(e))[:200]}
    store.push_history(from_id, "assistant", reply)
    try:
        await send_whatsapp_async(from_id, reply, request_id)
    except Exception as e:
        # Ответ сгенерирован; 500 НЕ возвращаем (иначе Meta-ретрай = дубль, P2 #19)
        log.warning("rid=%s reply generated but send failed: %s", request_id, scrub(str(e))[:200])
        return {"status": "send-failed", "model": model, "error": scrub(str(e))[:200]}
    if _is_lead(text, reply):
        snippet = " | ".join(m["content"] for m in hist[-3:])[:400]
        await notify_admin_telegram_async(snippet, from_id, request_id)
    try:
        log_lead(hist + [{"role": "assistant", "content": reply}])
    except Exception:
        pass
    log.info("rid=%s done dt=%.2f from=%s model=%s", request_id, time.time() - t0,
             mask_phone(from_id), model)
    return {"status": "ok", "model": model}


async def run_static_reply(to: str, request_id="-") -> dict:
    try:
        await send_whatsapp_async(to, UNSUPPORTED_REPLY, request_id)
        return {"status": "static-sent"}
    except Exception as e:
        log.warning("rid=%s static reply failed: %s", request_id, scrub(str(e))[:150])
        return {"status": "static-failed", "error": scrub(str(e))[:150]}


# ---------- полный pipeline (совместимость: старые вызывающие, тесты) ----------
async def process_whatsapp_message_async(body: dict, raw_body: bytes, signature: str,
                                         request_id="-", _store=None):
    from store import ProdStore
    store = _store or getattr(process_whatsapp_message_async, "_dstore", None)
    if store is None:
        store = ProdStore()
        process_whatsapp_message_async._dstore = store
    decision, data = precheck_webhook(store, body, raw_body, signature)
    if decision == "ack_only":
        return data.get("http_code", 200), {"status": data.get("status")}
    if decision == "send_static":
        res = await run_static_reply(data["from_id"], request_id)
        return 200, {"status": res["status"]}
    res = await run_claimed_text(store, data["from_id"], data["text"],
                                 data.get("contact_name", "Unknown"), request_id)
    # Важно: всегда 200 после генерации (см. P2 #19) — только authent/signature дают 4xx/503
    return 200, res


def process_whatsapp_message(body: dict, raw_body: bytes, signature: str):
    return _run_sync(process_whatsapp_message_async(body, raw_body, signature))


if __name__ == "__main__":
    print("Имплант-Дент DEMO — Gemini. Пиши 'выход' для завершения.\n")
    history = []
    try:
        reply, model = call_llm(build_messages([{"role": "user", "content": "Привет! Начни по сценарию: ты пишешь первым."}]))
        print(f"[бот/{model}]: {reply}\n")
        history.append({"role": "assistant", "content": reply})
    except Exception as e:
        print("Ошибка старта:", e)
    while True:
        try:
            user = input("Вы: ").strip()
        except EOFError:
            break
        if not user:
            continue
        if user.lower() in ("выход", "exit", "quit"):
            break
        history.append({"role": "user", "content": user})
        try:
            reply, model = call_llm(build_messages(history))
            print(f"\n[бот/{model}]: {reply}\n")
            history.append({"role": "assistant", "content": reply})
            log_lead(history)
        except Exception as e:
            print("Ошибка:", e)
