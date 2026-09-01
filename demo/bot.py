"""
bot.py — ядро демо-бота Имплант-Дент (Gemini primary, OpenRouter fallback)
- держит SYSTEM_PROMPT из PROMPT_Имплант-Дент.md
- ходит в Gemini (gemini-3.6-flash, free 1500/d) с fallback по моделям + OpenRouter
- ведёт историю диалога, логирует лиды
"""
import json, time, re, sys
from pathlib import Path
import requests
from config import GEMINI_API_KEY, GEMINI_MODELS, OPENROUTER_API_KEY, OPENROUTER_MODELS, OPENROUTER_REFERER, OPENROUTER_TITLE, SYSTEM_PROMPT, LEADS_FILE

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

def build_messages(history):
    """history: list of {"role": "user"/"assistant", "content": str}"""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(history)
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

def log_lead(history, meta=None):
    name, phone = extract_lead(history)
    if name or phone:
        rec = {"ts": int(time.time()), "name": name, "phone": phone, "history": history[-6:], "meta": meta}
        with open(LEADS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

# alias для совместимости со старым app.py
call_openrouter_compat = call_llm

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
