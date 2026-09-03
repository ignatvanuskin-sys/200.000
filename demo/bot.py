"""
bot.py — ядро демо-бота Имплант-Дент (Gemini primary, OpenRouter fallback)
- держит SYSTEM_PROMPT из PROMPT_Имплант-Дент.md
- ходит в Gemini (gemini-3.6-flash, free 1500/d) с fallback по моделям + OpenRouter
- ведёт историю диалога, логирует лиды
"""
import json, time, re, sys, hashlib, hmac
from pathlib import Path
import requests
from config import (
    GEMINI_API_KEY, GEMINI_MODELS, OPENROUTER_API_KEY, OPENROUTER_MODELS,
    OPENROUTER_REFERER, OPENROUTER_TITLE, SYSTEM_PROMPT, LEADS_FILE,
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_APP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DENTICA_PROMPT,
)

# Windows cp1251 fix
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except: pass

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

def _gemini_payload(messages, max_tokens=600, temperature=0.6):
    # messages[0] is system, rest is history
    system_text = messages[0]["content"] if messages and messages[0].get("role")=="system" else ""
    history = messages[1:] if system_text else messages
    contents = []
    for m in history:
        role = "model" if m.get("role")=="assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content","")}]})
    # Gemini требует хотя бы один user; если история пуста — добавим заглушку
    if not contents:
        contents = [{"role":"user","parts":[{"text":"Привет"}]}]
    payload = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}
    }
    return payload

def call_gemini(messages, temperature=0.6, max_tokens=600):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    last_err = None
    for model in GEMINI_MODELS:
        # model name may be with or without prefix models/
        mname = model.replace("models/","")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mname}:generateContent"
        payload = _gemini_payload(messages, max_tokens=max_tokens, temperature=temperature)
        try:
            r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json()
                # gemini response: candidates[0].content.parts[0].text
                cand = data.get("candidates", [{}])[0]
                parts = cand.get("content", {}).get("parts", [])
                text = "".join(p.get("text","") for p in parts).strip()
                if not text:
                    last_err = f"{model} empty response {str(data)[:400]}"
                    continue
                return text, model
            else:
                last_err = f"{model} -> {r.status_code} {r.text[:400]}"
                # 404 = модель не найдена, пробуем след; 429 квота — пробуем lighter
                continue
        except Exception as e:
            last_err = f"{model} exception {e}"
            continue
    raise RuntimeError(f"Gemini все модели недоступны. Последняя ошибка: {last_err}")

def call_openrouter(messages, temperature=0.6, max_tokens=600):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    }
    last_err = None
    for model in OPENROUTER_MODELS:
        if not OPENROUTER_API_KEY:
            break
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                return content, model
            else:
                last_err = f"{model} -> {r.status_code} {r.text[:300]}"
                continue
        except Exception as e:
            last_err = f"{model} exception {e}"
            continue
    raise RuntimeError(f"OpenRouter недоступен: {last_err}")

def call_llm(messages, temperature=0.6, max_tokens=600):
    """Gemini primary, OpenRouter fallback"""
    try:
        return call_gemini(messages, temperature, max_tokens)
    except Exception as e_gem:
        # пробуем OpenRouter только если есть ключ
        if OPENROUTER_API_KEY:
            try:
                return call_openrouter(messages, temperature, max_tokens)
            except Exception as e_or:
                raise RuntimeError(f"Gemini fail: {e_gem} | OpenRouter fail: {e_or}")
        raise

def _sanitize(text: str, limit=2000) -> str:
    return text.strip()[:limit]

def build_messages(history):
    """history: list of {"role": "user"/"assistant", "content": str} — санитизируем"""
    clean = []
    for m in history[-16:]:
        role = m.get("role","user")
        if role not in ("user","assistant","system"):
            role = "user"
        content = _sanitize(m.get("content","") or "")
        if content:
            clean.append({"role": role, "content": content})
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(clean)
    return msgs

def extract_lead(history):
    text = " ".join(m["content"] for m in history if m["role"] == "user")
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

import threading
_lead_lock = threading.Lock()
def log_lead(history, meta=None):
    name, phone = extract_lead(history)
    if not (name or phone):
        return
    # privacy: не логируем полный номер в консоль, только маскированный
    try:
        # rotation 1MB → leads.1.jsonl
        if LEADS_FILE.exists() and LEADS_FILE.stat().st_size > 1_000_000:
            backup = LEADS_FILE.with_suffix(".1.jsonl")
            try:
                if backup.exists(): backup.unlink()
                LEADS_FILE.rename(backup)
            except: pass
        rec = {"ts": int(time.time()), "name": name, "phone": phone, "history": history[-6:], "meta": meta}
        # file lock для Railway multi-worker (best effort)
        with _lead_lock:
            with open(LEADS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[lead log error] {e}")

# alias для совместимости со старым app.py
call_openrouter_compat = call_llm

# ===================== WhatsApp Cloud API =====================

# In-memory хранилища (для прод нужен Redis/KV)
_wa_rate = {}       # phone -> [timestamps]
_wa_seen = {}       # msg_id -> timestamp (дедупликация)
_wa_history = {}   # phone -> [{role, content, ts}]

def _is_wa_rate_limited(from_id: str, limit=10, window_s=60) -> bool:
    now = time.time()
    arr = _wa_rate.get(from_id, [])
    fresh = [t for t in arr if now - t < window_s]
    if len(fresh) >= limit:
        return True
    fresh.append(now)
    _wa_rate[from_id] = fresh
    return False

def _is_wa_duplicate(msg_id: str) -> bool:
    if not msg_id:
        return False
    if msg_id in _wa_seen:
        return True
    _wa_seen[msg_id] = time.time()
    # cleanup старше 5 минут
    now = time.time()
    for k in list(_wa_seen.keys()):
        if now - _wa_seen[k] > 300:
            del _wa_seen[k]
    return False

def _get_wa_history(from_id: str):
    h = _wa_history.get(from_id, [])
    now = time.time()
    fresh = [m for m in h if now - m.get("ts", 0) < 24 * 3600]
    if len(fresh) != len(h):
        _wa_history[from_id] = fresh
    return [{"role": m["role"], "content": m["content"]} for m in fresh]

def _push_wa_history(from_id: str, role: str, content: str):
    arr = _wa_history.get(from_id, [])
    arr.append({"role": role, "content": content, "ts": time.time()})
    if len(arr) > 10:
        arr.pop(0)
    _wa_history[from_id] = arr

def _verify_wa_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Проверка X-Hub-Signature-256 от Meta."""
    if not secret:
        return True  # если секрет не задан — пропускаем
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)

def _notify_admin_telegram(text: str, from_id: str):
    """Уведомление админа в Telegram о новом лиде."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[notify skip] no telegram config, from=...{from_id[-4:]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"📥 Заявка ...{from_id[-4:]}: {text[:500]}"},
            timeout=10,
        )
    except Exception as e:
        print(f"[notify fail] {str(e)[:200]}")

def send_whatsapp(to: str, text: str):
    """Отправка текстового сообщения в WhatsApp Cloud API."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID not set")
    if not re.match(r"^\d{7,15}$", to):
        raise ValueError(f"invalid phone: {to}")
    r = requests.post(
        f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers={
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4000]},
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"wa send {r.status_code} {r.text[:300]}")
    return r.json()

def process_whatsapp_message(body: dict, raw_body: bytes, signature: str):
    """
    Обработка входящего webhook от WhatsApp Cloud API.
    Возвращает (status_code, response_dict).
    """
    # Проверка подписи
    if WHATSAPP_APP_SECRET and not _verify_wa_signature(raw_body, signature, WHATSAPP_APP_SECRET):
        print("[wa] signature verification failed")
        return 401, {"error": "invalid signature"}

    entry = (body.get("entry") or [{}])[0]
    changes = (entry.get("changes") or [{}])[0]
    value = changes.get("value", {})
    msg = (value.get("messages") or [None])[0]

    if not msg:
        # status update или другой нерелевантный payload
        return 200, {"status": "ignored non-message"}

    if msg.get("type") != "text":
        return 200, {"status": "ignored non-text"}

    msg_id = msg.get("id", "")
    if _is_wa_duplicate(msg_id):
        print(f"[wa] duplicate {msg_id}")
        return 200, {"status": "duplicate"}

    from_id = msg.get("from", "")
    if not re.match(r"^\d{7,15}$", from_id):
        print(f"[wa] invalid from {from_id}")
        return 200, {"status": "invalid phone"}

    if _is_wa_rate_limited(from_id):
        print(f"[wa] rate limited {from_id[-4:]}")
        return 200, {"status": "rate limited"}

    text = (msg.get("text", {}).get("body", "") or "").strip()[:2000]
    if not text:
        return 200, {"status": "empty"}

    print(f"[wa] msg id={msg_id} from=...{from_id[-4:]} len={len(text)}")

    # История диалога (in-memory, 10 сообщений, 24ч)
    _push_wa_history(from_id, "user", text)
    hist = _get_wa_history(from_id)
    all_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + hist

    try:
        reply, _model = call_llm(all_messages)
        _push_wa_history(from_id, "assistant", reply)
        send_whatsapp(from_id, reply)

        # Уведомление админу если нашли телефон/имя или бот сказал "Передал"
        has_phone = bool(re.search(r"\+7|8\s*\(?\d{3}", text) or re.search(r"\+7|8\s*\(?\d{3}", reply))
        if has_phone or "Передал администратору" in reply:
            snippet = " | ".join(m["content"] for m in hist[-3:])[:400]
            _notify_admin_telegram(snippet, from_id)

    except Exception as e:
        print(f"[wa error] {str(e)[:500]}")
        try:
            send_whatsapp(from_id, "Сбой, попробуйте ещё раз. Оператор свяжется.")
        except:
            pass
        return 500, {"error": "processing error"}

    return 200, {"status": "ok"}


if __name__ == "__main__":
    print("Имплант-Дент DEMO — Gemini. Пиши 'выход' для завершения.\n")
    history = []
    try:
        reply, model = call_llm(build_messages([{"role":"user","content":"Привет! Начни по сценарию: ты пишешь первым."}]))
        print(f"[бот/{model}]: {reply}\n")
        history.append({"role":"assistant","content":reply})
    except Exception as e:
        print("Ошибка старта:", e)
    while True:
        try:
            user = input("Вы: ").strip()
        except EOFError:
            break
        if not user:
            continue
        if user.lower() in ("выход","exit","quit"):
            break
        history.append({"role":"user","content":user})
        try:
            reply, model = call_llm(build_messages(history))
            print(f"\n[бот/{model}]: {reply}\n")
            history.append({"role":"assistant","content":reply})
            log_lead(history)
        except Exception as e:
            print("Ошибка:", e)
